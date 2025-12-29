#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket市场数据统计脚本
获取配置文件中市场的详细数据并以表格形式展示
"""

import json
import os
import csv
import requests
import webbrowser
from datetime import datetime
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.progress import Progress
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OpenOrderParams

# 加载环境变量
load_dotenv()

# Polymarket API基础URL
CLOB_API_BASE = "https://clob.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"

# 交易配置
HOST = "https://clob.polymarket.com"
PK = os.getenv("PK")
CHAIN_ID = 137
PROXY_ADDRESS = os.getenv("PROXY_ADDRESS")

console = Console()


# ============= 辅助函数 =============

def normalize_price(price: float, tick_size: float) -> float:
    """标准化价格到tick_size的倍数"""
    return round(price / tick_size) * tick_size


def get_order_remaining_size(order: Dict) -> float:
    """获取订单剩余数量"""
    return float(order['original_size']) - float(order['size_matched'])


def get_my_sizes_by_price(orders: List[Dict], tick_size: float) -> Dict[float, float]:
    """计算每个价格上自己的订单总量"""
    sizes = {}
    for order in orders:
        price = normalize_price(float(order['price']), tick_size)
        sizes[price] = sizes.get(price, 0) + get_order_remaining_size(order)
    return sizes

def aggregate_other_liquidity(orderbook_side, my_sizes: Dict[float, float], tick_size: float, descending: bool = True):
    """汇总除自己外每档的数量并排序"""
    aggregated = {}
    for level in orderbook_side:
        price = normalize_price(float(level.price), tick_size)
        other_size = float(level.size) - my_sizes.get(price, 0)
        if other_size <= 0:
            continue
        aggregated[price] = aggregated.get(price, 0) + other_size
    sorted_prices = sorted(aggregated.keys(), reverse=descending)
    return [(p, aggregated[p]) for p in sorted_prices]


def find_price_by_value(levels: List, target_value: float, is_bid: bool = True) -> float:
    """从最优档开始累加金额，找到累计金额>=目标金额的价格"""
    if not levels:
        return 0.0 if is_bid else 1.0
    if target_value <= 0:
        return levels[0][0]
    cumulative = 0.0
    last_price = levels[0][0]
    for price, size in levels:
        last_price = price
        cumulative += price * size
        if cumulative >= target_value:
            return price
    return last_price


def cumulative_value_to_price(levels: List, price_limit: float, is_bid: bool = True) -> float:
    """
    计算从最优价到给定价格（含）之间的累计金额（其他人订单）
    levels已按方向排序（买：高->低，卖：低->高）
    """
    if not levels:
        return 0.0
    cumulative = 0.0
    for price, size in levels:
        cumulative += price * size
        # 对买盘，当价格低于目标价时退出；卖盘则当价格高于目标价时退出
        if (is_bid and price <= price_limit) or (not is_bid and price >= price_limit):
            break
    return cumulative


def load_markets_config() -> List[Dict]:
    """加载市场配置文件"""
    config_path = os.path.join(os.path.dirname(__file__), 'markets_config.json')
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)
    return config['markets']


def get_market_slug(market_id: str) -> Optional[str]:
    """
    通过CLOB API获取市场的slug

    Args:
        market_id: 市场的condition_id（十六进制格式）

    Returns:
        市场的slug，如果获取失败则返回None
    """
    url = f"{CLOB_API_BASE}/markets/{market_id}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get('market_slug')
    except Exception as e:
        console.print(f"[red]获取市场slug失败 {market_id[:10]}...: {str(e)}[/red]")
        return None


def get_orderbook_depth(token_id: str, client: Optional[ClobClient] = None,
                        market_id: Optional[str] = None) -> Dict:
    """
    获取订单簿深度信息（最佳买价和卖价的订单量和金额）
    如果提供了client，会排除用户自己的订单

    Args:
        token_id: token ID
        client: ClobClient实例（可选）
        market_id: 市场ID（可选，用于获取用户订单）

    Returns:
        包含最佳买价/卖价的订单量和金额，以及价格
    """
    result = {
        'best_bid': 0.0,
        'best_ask': 1.0,
        'best_bid_size': 0,
        'best_bid_value': 0,
        'best_ask_size': 0,
        'best_ask_value': 0,
        'target_buy_price': 0.0,
        'target_sell_price': 1.0,
        'buy_cum_value': 0.0,
        'sell_cum_value': 0.0,
        'inside_buy_value': 0.0,
        'inside_sell_value': 0.0,
        'tick_size': 0.0
    }

    if not client:
        return result

    try:
        # 获取tick_size
        tick_size = float(client.get_tick_size(token_id))

        # 获取订单簿
        orderbook = client.get_order_book(token_id)

        # 获取用户自己的订单
        my_buy_sizes = {}
        my_sell_sizes = {}

        if market_id:
            try:
                active_orders = client.get_orders(OpenOrderParams(market=market_id))
                buy_orders = [o for o in active_orders if o['side'] == 'BUY']
                sell_orders = [o for o in active_orders if o['side'] == 'SELL']

                my_buy_sizes = get_my_sizes_by_price(buy_orders, tick_size)
                my_sell_sizes = get_my_sizes_by_price(sell_orders, tick_size)
            except:
                pass

        bid_levels = aggregate_other_liquidity(orderbook.bids, my_buy_sizes, tick_size, descending=True)
        ask_levels = aggregate_other_liquidity(orderbook.asks, my_sell_sizes, tick_size, descending=False)

        if bid_levels:
            result['best_bid'], top_bid_size = bid_levels[0]
            result['best_bid_size'] = top_bid_size
            result['best_bid_value'] = result['best_bid'] * top_bid_size
        if ask_levels:
            result['best_ask'], top_ask_size = ask_levels[0]
            result['best_ask_size'] = top_ask_size
            result['best_ask_value'] = result['best_ask'] * top_ask_size

        result['tick_size'] = tick_size

        return {**result, 'bid_levels': bid_levels, 'ask_levels': ask_levels}

    except Exception as e:
        # console.print(f"[red]获取订单簿失败: {str(e)}[/red]")
        return result


def get_market_data(market_id: str, slug: Optional[str] = None) -> Dict:
    """
    通过API获取单个市场的详细数据

    策略：
    1. 如果没有slug，先通过CLOB API获取slug
    2. 使用slug从Gamma API获取完整的市场数据（包括交易量等）

    Args:
        market_id: 市场的condition_id
        slug: 市场的slug（可选，如果提供则跳过第一步）

    Returns:
        市场详细数据字典
    """
    # 第一步：获取slug（如果没有提供）
    if not slug:
        slug = get_market_slug(market_id)
        if not slug:
            return {}

    # 第二步：通过slug获取完整市场数据
    url = f"{GAMMA_API_BASE}/markets/slug/{slug}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        console.print(f"[red]获取市场数据失败 {slug}: {str(e)}[/red]")
        return {}


def format_volume(volume: float) -> str:
    """格式化交易量显示"""
    if volume >= 1_000_000:
        return f"${volume/1_000_000:.2f}M"
    elif volume >= 1_000:
        return f"${volume/1_000:.1f}K"
    else:
        return f"${volume:.0f}"


def format_price_change(change: float) -> str:
    """格式化价格变化显示"""
    if change is None or change == 0:
        return "-"

    color = "green" if change > 0 else "red"
    sign = "+" if change > 0 else ""
    return f"[{color}]{sign}{change*100:.1f}%[/{color}]"


def extract_market_stats(market_data: Dict, market_config: Dict,
                        client: Optional[ClobClient] = None) -> Dict:
    """
    从API数据中提取关键统计信息

    Args:
        market_data: API返回的市场数据
        market_config: 配置文件中的市场信息
        client: ClobClient实例（可选，用于排除用户自己的订单）

    Returns:
        提取的关键数据
    """
    if not market_data:
        return {
            'name': market_config.get('name', 'Unknown'),
            'enabled': market_config.get('enabled', False),
            'error': True
        }

    # 获取订单簿深度信息（根据trade_side选择对应的token_id）
    trade_side = market_config.get('trade_side', 'no')
    token_id = market_config.get('yes_token_id') if trade_side == 'yes' else market_config.get('no_token_id')
    market_id = market_config.get('market_id')

    orderbook_depth = {
        'best_bid': 0.0,
        'best_ask': 1.0,
        'best_bid_value': 0,
        'best_ask_value': 0,
        'best_bid_size': 0,
        'best_ask_size': 0
    }
    if token_id:
        # 传递client和market_id，这样就会排除用户自己的订单
        orderbook_depth = get_orderbook_depth(token_id, client, market_id)

    # 计算派生指标（按目标金额定位档位）
    target_value = float(market_config.get('max_position_value', 25))
    best_bid = orderbook_depth.get('best_bid', 0)
    best_ask = orderbook_depth.get('best_ask', 0)

    bid_levels = orderbook_depth.get('bid_levels', [])
    ask_levels = orderbook_depth.get('ask_levels', [])

    target_buy_price = find_price_by_value(bid_levels, target_value, is_bid=True)
    target_sell_price = find_price_by_value(ask_levels, target_value, is_bid=False)

    buy_cum_value = cumulative_value_to_price(bid_levels, target_buy_price, is_bid=True)
    sell_cum_value = cumulative_value_to_price(ask_levels, target_sell_price, is_bid=False)

    # 内侧金额：从买一/卖一到目标档位之间的他人挂单金额
    inside_buy_value = buy_cum_value if bid_levels else 0
    inside_sell_value = sell_cum_value if ask_levels else 0

    volume_1w = market_data.get('volume1wk', 0)

    # 价差（目标档位之间）
    spread = target_sell_price - target_buy_price if target_sell_price > target_buy_price else 0

    # 周转率基于卖侧内侧金额做近似
    volume_24h = market_data.get('volume24hr', 0)
    base_value = inside_sell_value if inside_sell_value > 0 else orderbook_depth.get('best_ask_value', 0)
    turnover_ratio = (volume_1w) / (base_value + target_value) if base_value > 0 else 0

    # 收益率 = 价差 * 周转率
    yield_rate = spread * turnover_ratio

    return {
        'name': market_config.get('name', market_data.get('question', 'Unknown')),
        'enabled': market_config.get('enabled', False),
        'trade_side': market_config.get('trade_side', 'N/A'),
        'volume_24h': market_data.get('volume24hr', 0),
        'volume_1w': volume_1w,
        'volume_total': market_data.get('volumeNum', 0),
        'liquidity': market_data.get('liquidityNum', 0),
        'last_price': market_data.get('lastTradePrice', 0),
        'price_change_24h': market_data.get('oneDayPriceChange', None),
        'price_change_1w': market_data.get('oneWeekPriceChange', None),
        'best_bid': best_bid,
        'best_ask': best_ask,
        'best_bid_value': inside_buy_value,
        'best_ask_value': inside_sell_value,
        'target_buy_price': target_buy_price,
        'target_sell_price': target_sell_price,
        'buy_cum_value': buy_cum_value,
        'sell_cum_value': sell_cum_value,
        'spread': spread,
        'turnover_ratio': turnover_ratio,
        'yield_rate': yield_rate,
        'active': market_data.get('active', False),
        'closed': market_data.get('closed', True),
        'end_date': market_data.get('endDateIso', 'N/A'),
        'error': False
    }


def display_summary_table(markets_stats: List[Dict]):
    """显示市场概览表格"""
    table = Table(title="🎯 Polymarket 市场数据概览", show_header=True, header_style="bold magenta",
                  box=None, padding=(0, 1))

    table.add_column("状态", style="dim", width=4, no_wrap=True)
    table.add_column("市场名称", style="cyan", width=35, no_wrap=False)
    table.add_column("方向", justify="center", width=5, no_wrap=True)
    table.add_column("24h量", justify="right", style="yellow", width=8)
    table.add_column("7d量", justify="right", style="yellow", width=8)
    table.add_column("流动性", justify="right", width=8)
    table.add_column("买价金额", justify="right", width=8)
    table.add_column("卖价金额", justify="right", width=8)
    table.add_column("价差", justify="right", width=6)
    table.add_column("周转率", justify="right", width=8)
    table.add_column("收益率", justify="right", width=8)

    # 按收益率排序（从大到小）
    sorted_stats = sorted(markets_stats, key=lambda x: x.get('yield_rate', 0), reverse=True)

    for stat in sorted_stats:
        if stat.get('error'):
            continue

        status = "✅" if stat.get('enabled') else "⏸️"
        name = stat['name'][:37] + "..." if len(stat['name']) > 40 else stat['name']
        side = stat['trade_side'].upper()[:3]

        # 市场状态指示
        if stat.get('closed'):
            status = "🔒"
        elif not stat.get('active'):
            status = "⚠️"

        # 格式化价差
        spread = stat.get('spread', 0)
        spread_str = f"{spread:.3f}" if spread > 0 else "-"

        # 格式化周转率
        turnover = stat.get('turnover_ratio', 0)
        if turnover >= 1:
            turnover_str = f"{turnover:.1f}x"
        elif turnover > 0:
            turnover_str = f"{turnover:.2f}x"
        else:
            turnover_str = "-"

        # 格式化收益率
        yield_rate = stat.get('yield_rate', 0)
        if yield_rate >= 1:
            yield_str = f"{yield_rate:.1f}"
        elif yield_rate > 0:
            yield_str = f"{yield_rate:.2f}"
        else:
            yield_str = "-"

        table.add_row(
            status,
            name,
            side,
            format_volume(stat['volume_24h']),
            format_volume(stat['volume_1w']),
            format_volume(stat['liquidity']),
            format_volume(stat.get('best_bid_value', 0)),
            format_volume(stat.get('best_ask_value', 0)),
            spread_str,
            turnover_str,
            yield_str
        )

    console.print(table)


def display_detailed_stats(markets_stats: List[Dict]):
    """显示详细市场统计"""
    # 只显示启用的市场
    enabled_markets = [m for m in markets_stats if m.get('enabled') and not m.get('error')]

    if not enabled_markets:
        console.print("[yellow]没有启用的市场[/yellow]")
        return

    console.print("\n📊 启用市场详细数据\n", style="bold blue")

    for stat in enabled_markets:
        console.print(f"[bold cyan]{stat['name']}[/bold cyan] [{stat['trade_side'].upper()}]")
        console.print(f"  💰 交易量: 24h={format_volume(stat['volume_24h'])} | "
                     f"7d={format_volume(stat['volume_1w'])} | "
                     f"总计={format_volume(stat['volume_total'])}")
        console.print(f"  📈 价格: 最新={stat['last_price']:.3f} | "
                     f"买盘={stat['best_bid']:.3f} | "
                     f"卖盘={stat['best_ask']:.3f}")
        console.print(f"  📊 变化: 24h={format_price_change(stat['price_change_24h'])} | "
                     f"7d={format_price_change(stat['price_change_1w'])}")
        console.print(f"  💧 流动性: {format_volume(stat['liquidity'])}")
        console.print(f"  ⏰ 结束时间: {stat['end_date']}")
        console.print()


def display_statistics_summary(markets_stats: List[Dict]):
    """显示总体统计摘要"""
    enabled_markets = [m for m in markets_stats if m.get('enabled') and not m.get('error')]
    all_markets = [m for m in markets_stats if not m.get('error')]

    total_volume_24h = sum(m.get('volume_24h', 0) for m in enabled_markets)
    total_volume_total = sum(m.get('volume_total', 0) for m in all_markets)
    total_liquidity = sum(m.get('liquidity', 0) for m in enabled_markets)

    table = Table(title="📈 总体统计", show_header=True, header_style="bold green")
    table.add_column("指标", style="cyan")
    table.add_column("数值", justify="right", style="yellow")

    table.add_row("监控市场总数", str(len(all_markets)))
    table.add_row("启用市场数量", str(len(enabled_markets)))
    table.add_row("启用市场24h总交易量", format_volume(total_volume_24h))
    table.add_row("所有市场累计交易量", format_volume(total_volume_total))
    table.add_row("启用市场总流动性", format_volume(total_liquidity))

    console.print("\n")
    console.print(table)


def save_to_csv(markets_stats: List[Dict], filename: Optional[str] = None):
    """
    将市场数据保存为CSV文件

    Args:
        markets_stats: 市场统计数据列表
        filename: 输出文件名（可选，默认使用时间戳）
    """
    if filename is None:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"polymarket_markets_{timestamp}.csv"

    # 过滤掉错误的数据
    valid_stats = [s for s in markets_stats if not s.get('error')]

    if not valid_stats:
        console.print("[yellow]没有有效数据可保存[/yellow]")
        return

    # 按收益率排序（从大到小）
    sorted_stats = sorted(valid_stats, key=lambda x: x.get('yield_rate', 0), reverse=True)

    # 定义CSV列
    fieldnames = [
        '状态', '市场名称', '交易方向',
        '24h交易量', '7d交易量', '总交易量',
        '流动性',
        '最佳买价(%)', '最佳卖价(%)',
        '最佳买价金额', '最佳卖价金额',
        '价差(%)', '周转率', '收益率'
    ]

    try:
        with open(filename, 'w', newline='', encoding='utf-8-sig') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()

            for stat in sorted_stats:
                # 确定状态图标
                if stat.get('closed'):
                    status = '已关闭'
                elif not stat.get('active'):
                    status = '未激活'
                elif stat.get('enabled'):
                    status = '已启用'
                else:
                    status = '已暂停'

                row = {
                    '状态': status,
                    '市场名称': stat['name'],
                    '交易方向': stat['trade_side'].upper(),
                    '24h交易量': f"{stat['volume_24h']:.2f}",
                    '7d交易量': f"{stat['volume_1w']:.2f}",
                    '总交易量': f"{stat['volume_total']:.2f}",
                    '流动性': f"{stat['liquidity']:.2f}",
                    '最佳买价(%)': f"{stat['best_bid']*100:.1f}" if stat['best_bid'] else '',
                    '最佳卖价(%)': f"{stat['best_ask']*100:.1f}" if stat['best_ask'] else '',
                    '最佳买价金额': f"{stat.get('best_bid_value', 0):.2f}",
                    '最佳卖价金额': f"{stat.get('best_ask_value', 0):.2f}",
                    '价差(%)': f"{stat.get('spread', 0)*100:.1f}" if stat.get('spread', 0) > 0 else '',
                    '周转率': f"{stat.get('turnover_ratio', 0):.4f}" if stat.get('turnover_ratio', 0) > 0 else '',
                    '收益率': f"{stat.get('yield_rate', 0):.4f}" if stat.get('yield_rate', 0) > 0 else ''
                }
                writer.writerow(row)

        console.print(f"\n[green]✅ 数据已保存到: {filename}[/green]")
        console.print(f"[dim]共保存 {len(sorted_stats)} 个市场的数据[/dim]")
        return filename

    except Exception as e:
        console.print(f"[red]❌ 保存CSV文件失败: {str(e)}[/red]")
        return None


def save_to_html(markets_stats: List[Dict], filename: str = "polymarket_markets.html"):
    """
    将市场数据保存为HTML文件并自动打开

    Args:
        markets_stats: 市场统计数据列表
        filename: 输出文件名（默认为polymarket_markets.html）
    """
    # 过滤掉错误的数据
    valid_stats = [s for s in markets_stats if not s.get('error')]

    if not valid_stats:
        console.print("[yellow]没有有效数据可保存[/yellow]")
        return

    # 按收益率排序（从大到小）
    sorted_stats = sorted(valid_stats, key=lambda x: x.get('yield_rate', 0), reverse=True)

    # 计算总体统计
    enabled_markets = [m for m in valid_stats if m.get('enabled')]
    total_volume_24h = sum(m.get('volume_24h', 0) for m in enabled_markets)
    total_volume_total = sum(m.get('volume_total', 0) for m in valid_stats)
    total_liquidity = sum(m.get('liquidity', 0) for m in enabled_markets)

    # 生成HTML
    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket 市场数据 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            padding: 20px;
            min-height: 100vh;
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }}
        .header h1 {{
            font-size: 32px;
            margin-bottom: 10px;
        }}
        .header p {{
            font-size: 16px;
            opacity: 0.9;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            padding: 30px;
            background: #f8f9fa;
            border-bottom: 2px solid #e9ecef;
        }}
        .stat-card {{
            background: white;
            padding: 20px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            text-align: center;
        }}
        .stat-card h3 {{
            font-size: 14px;
            color: #6c757d;
            margin-bottom: 8px;
        }}
        .stat-card p {{
            font-size: 24px;
            font-weight: bold;
            color: #495057;
        }}
        .table-container {{
            padding: 30px;
            overflow-x: auto;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: white;
            font-size: 14px;
        }}
        thead {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            position: sticky;
            top: 0;
            z-index: 10;
        }}
        th {{
            padding: 12px 8px;
            text-align: left;
            font-weight: 600;
            white-space: nowrap;
        }}
        th.right {{
            text-align: right;
        }}
        tbody tr {{
            border-bottom: 1px solid #e9ecef;
            transition: background-color 0.2s;
        }}
        tbody tr:hover {{
            background-color: #f8f9fa;
        }}
        td {{
            padding: 12px 8px;
            white-space: nowrap;
        }}
        td.right {{
            text-align: right;
        }}
        .status {{
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
        }}
        .status.enabled {{
            background: #d4edda;
            color: #155724;
        }}
        .status.paused {{
            background: #fff3cd;
            color: #856404;
        }}
        .status.closed {{
            background: #f8d7da;
            color: #721c24;
        }}
        .positive {{
            color: #28a745;
            font-weight: 600;
        }}
        .negative {{
            color: #dc3545;
            font-weight: 600;
        }}
        .trade-side {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
        }}
        .trade-side.yes {{
            background: #d4edda;
            color: #155724;
        }}
        .trade-side.no {{
            background: #f8d7da;
            color: #721c24;
        }}
        .footer {{
            padding: 20px;
            text-align: center;
            color: #6c757d;
            background: #f8f9fa;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎯 Polymarket 市场数据概览</h1>
            <p>更新时间: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}</p>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <h3>监控市场总数</h3>
                <p>{len(valid_stats)}</p>
            </div>
            <div class="stat-card">
                <h3>启用市场数量</h3>
                <p>{len(enabled_markets)}</p>
            </div>
            <div class="stat-card">
                <h3>启用市场24h总交易量</h3>
                <p>{format_volume(total_volume_24h)}</p>
            </div>
            <div class="stat-card">
                <h3>所有市场累计交易量</h3>
                <p>{format_volume(total_volume_total)}</p>
            </div>
            <div class="stat-card">
                <h3>启用市场总流动性</h3>
                <p>{format_volume(total_liquidity)}</p>
            </div>
        </div>

        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th>状态</th>
                        <th>市场名称</th>
                        <th>方向</th>
                        <th class="right">24h交易量</th>
                        <th class="right">7d交易量</th>
                        <th class="right">流动性</th>
                        <th class="right">最佳买价</th>
                        <th class="right">买价金额</th>
                        <th class="right">最佳卖价</th>
                        <th class="right">卖价金额</th>
                        <th class="right">价差</th>
                        <th class="right">周转率</th>
                        <th class="right">收益率</th>
                    </tr>
                </thead>
                <tbody>
"""

    for stat in sorted_stats:
        # 确定状态
        if stat.get('closed'):
            status_class = 'closed'
            status_text = '已关闭'
        elif not stat.get('active'):
            status_class = 'paused'
            status_text = '未激活'
        elif stat.get('enabled'):
            status_class = 'enabled'
            status_text = '已启用'
        else:
            status_class = 'paused'
            status_text = '已暂停'

        # 交易方向
        trade_side = stat['trade_side'].upper()
        side_class = 'yes' if trade_side == 'YES' else 'no'

        # 周转率
        turnover = stat.get('turnover_ratio', 0)
        if turnover >= 1:
            turnover_str = f"{turnover:.1f}x"
        elif turnover > 0:
            turnover_str = f"{turnover:.2f}x"
        else:
            turnover_str = "-"

        # 预先格式化价格值
        best_bid_str = f"{stat['best_bid']*100:.1f}%" if stat['best_bid'] else '-'
        best_ask_str = f"{stat['best_ask']*100:.1f}%" if stat['best_ask'] else '-'
        spread_str = f"{stat.get('spread', 0)*100:.1f}%" if stat.get('spread', 0) > 0 else '-'

        # 格式化收益率
        yield_rate = stat.get('yield_rate', 0)
        if yield_rate >= 1:
            yield_str = f"{yield_rate:.1f}"
        elif yield_rate > 0:
            yield_str = f"{yield_rate:.2f}"
        else:
            yield_str = "-"

        html_content += f"""
                    <tr>
                        <td><span class="status {status_class}">{status_text}</span></td>
                        <td>{stat['name']}</td>
                        <td><span class="trade-side {side_class}">{trade_side}</span></td>
                        <td class="right">{format_volume(stat['volume_24h'])}</td>
                        <td class="right">{format_volume(stat['volume_1w'])}</td>
                        <td class="right">{format_volume(stat['liquidity'])}</td>
                        <td class="right">{best_bid_str}</td>
                        <td class="right">{format_volume(stat.get('best_bid_value', 0))}</td>
                        <td class="right">{best_ask_str}</td>
                        <td class="right">{format_volume(stat.get('best_ask_value', 0))}</td>
                        <td class="right">{spread_str}</td>
                        <td class="right">{turnover_str}</td>
                        <td class="right">{yield_str}</td>
                    </tr>
"""

    html_content += """
                </tbody>
            </table>
        </div>

        <div class="footer">
            <p>数据来源: Polymarket API | 自动生成于 """ + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + """</p>
        </div>
    </div>
</body>
</html>
"""

    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(html_content)

        console.print(f"\n[green]✅ HTML已保存到: {filename}[/green]")

        # 自动在浏览器中打开
        file_path = os.path.abspath(filename)
        webbrowser.open(f'file://{file_path}')
        console.print(f"[green]🌐 已在浏览器中打开[/green]")

        return filename

    except Exception as e:
        console.print(f"[red]❌ 保存HTML文件失败: {str(e)}[/red]")
        return None



def process_single_market(market_config: Dict, client: Optional[ClobClient] = None) -> Dict:
    """
    处理单个市场的数据获取

    Args:
        market_config: 市场配置
        client: ClobClient实例（可选）

    Returns:
        市场统计数据
    """
    try:
        market_id = market_config['market_id']
        market_data = get_market_data(market_id)
        return extract_market_stats(market_data, market_config, client)
    except Exception as e:
        console.print(f"[red]处理市场失败 {market_config.get('name', 'Unknown')}: {str(e)}[/red]")
        return {
            'name': market_config.get('name', 'Unknown'),
            'enabled': market_config.get('enabled', False),
            'error': True
        }


def main():
    """主函数"""
    console.print("\n[bold green]Polymarket 市场数据获取工具[/bold green]\n")
    console.print(f"⏰ 获取时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # 初始化客户端（用于排除自己的订单）
    client = None
    if PK and PROXY_ADDRESS:
        try:
            console.print("🔐 初始化交易客户端...")
            client = ClobClient(HOST, key=PK, chain_id=CHAIN_ID, signature_type=2, funder=PROXY_ADDRESS)
            client.set_api_creds(client.create_or_derive_api_creds())
            console.print("[green]✓ 客户端初始化成功（将排除您的订单）[/green]\n")
        except Exception as e:
            console.print(f"[yellow]⚠️ 客户端初始化失败，将显示所有订单: {str(e)}[/yellow]\n")
            client = None
    else:
        console.print("[yellow]⚠️ 未找到环境变量，将显示所有订单（包含您的订单）[/yellow]\n")

    # 加载配置
    markets_config = load_markets_config()
    console.print(f"📋 已加载 {len(markets_config)} 个市场配置\n")

    # 获取市场数据（使用多线程）
    markets_stats = []

    with Progress() as progress:
        task = progress.add_task("[cyan]正在获取市场数据...", total=len(markets_config))

        # 使用线程池并发获取市场数据
        with ThreadPoolExecutor(max_workers=10) as executor:
            # 提交所有任务
            future_to_market = {
                executor.submit(process_single_market, market_config, client): market_config
                for market_config in markets_config
            }

            # 收集结果
            for future in as_completed(future_to_market):
                try:
                    stats = future.result()
                    markets_stats.append(stats)
                except Exception as e:
                    market_config = future_to_market[future]
                    console.print(f"[red]获取市场数据失败 {market_config.get('name', 'Unknown')}: {str(e)}[/red]")
                    markets_stats.append({
                        'name': market_config.get('name', 'Unknown'),
                        'enabled': market_config.get('enabled', False),
                        'error': True
                    })
                finally:
                    progress.update(task, advance=1)

    # 显示表格
    display_summary_table(markets_stats)

    # 显示详细统计
    display_detailed_stats(markets_stats)

    # 显示总体统计
    display_statistics_summary(markets_stats)

    # 保存为HTML文件并自动打开
    save_to_html(markets_stats)

    console.print("\n[green]✅ 数据获取完成![/green]\n")


if __name__ == "__main__":
    main()
