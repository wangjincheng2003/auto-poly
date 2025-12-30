#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket 市场监控脚本
持续运行，定时采样收益率数据并生成HTML报告

用法:
  python market_monitor.py              # 默认1小时间隔
  python market_monitor.py -i 300       # 5分钟间隔（调试用）
"""

import argparse
import json
import os
import requests
import signal
import subprocess
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OpenOrderParams

load_dotenv()

# ============= 配置 =============

CLOB_API_BASE = "https://clob.polymarket.com"
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

PK = os.getenv("PK")
PROXY_ADDRESS = os.getenv("PROXY_ADDRESS")

SCRIPT_DIR = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(SCRIPT_DIR, 'markets_config.json')
HISTORY_FILE = os.path.join(SCRIPT_DIR, 'yield_history.json')
BACKUP_DIR = os.path.join(SCRIPT_DIR, 'yield_history_backup')
HTML_FILE = os.path.join(SCRIPT_DIR, 'polymarket_markets.html')

console = Console()
running = True


def signal_handler(signum, frame):
    global running
    console.print("\n[yellow]收到退出信号，正在停止...[/yellow]")
    running = False


# ============= 辅助函数 =============

def normalize_price(price: float, tick_size: float) -> float:
    return round(price / tick_size) * tick_size


def get_order_remaining_size(order: Dict) -> float:
    return float(order['original_size']) - float(order['size_matched'])


def get_my_sizes_by_price(orders: List[Dict], tick_size: float) -> Dict[float, float]:
    sizes = {}
    for order in orders:
        price = normalize_price(float(order['price']), tick_size)
        sizes[price] = sizes.get(price, 0) + get_order_remaining_size(order)
    return sizes


def aggregate_other_liquidity(orderbook_side, my_sizes: Dict[float, float], tick_size: float, descending: bool = True):
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
    if not levels:
        return 0.0
    cumulative = 0.0
    for price, size in levels:
        cumulative += price * size
        if (is_bid and price <= price_limit) or (not is_bid and price >= price_limit):
            break
    return cumulative


def format_volume(volume: float) -> str:
    if volume >= 1_000_000:
        return f"${volume/1_000_000:.2f}M"
    elif volume >= 1_000:
        return f"${volume/1_000:.1f}K"
    else:
        return f"${volume:.0f}"


# ============= 数据加载/保存 =============

def load_markets_config() -> List[Dict]:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
    return config['markets']


def load_history() -> Dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_history(history: Dict):
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def backup_old_data(history: Dict, days: int = 7) -> Dict:
    cutoff_time = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff_time.isoformat()

    old_data = {}
    new_history = {}

    for market_id, records in history.items():
        old_records = [r for r in records if r['ts'] < cutoff_str]
        new_records = [r for r in records if r['ts'] >= cutoff_str]
        if old_records:
            old_data[market_id] = old_records
        if new_records:
            new_history[market_id] = new_records

    if old_data:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        backup_file = os.path.join(BACKUP_DIR, f"yield_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(backup_file, 'w', encoding='utf-8') as f:
            json.dump(old_data, f, ensure_ascii=False, indent=2)
        console.print(f"[dim]备份旧数据: {backup_file}[/dim]")

    return new_history


def calculate_avg_yield(market_id: str, history: Dict, days: int = 7) -> Optional[float]:
    if market_id not in history or not history[market_id]:
        return None

    cutoff_time = datetime.now() - timedelta(days=days)
    cutoff_str = cutoff_time.isoformat()
    records = [r for r in history[market_id] if r['ts'] >= cutoff_str]

    if not records:
        records = history[market_id]

    if not records:
        return None

    yield_rates = [r['yield_rate'] for r in records]
    return sum(yield_rates) / len(yield_rates)


# ============= API 函数 =============

def get_market_slug(market_id: str) -> Optional[str]:
    url = f"{CLOB_API_BASE}/markets/{market_id}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json().get('market_slug')
    except:
        return None


def get_market_data(market_id: str, slug: Optional[str] = None) -> Dict:
    if not slug:
        slug = get_market_slug(market_id)
        if not slug:
            return {}

    url = f"{GAMMA_API_BASE}/markets/slug/{slug}"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except:
        return {}


def get_orderbook_depth(token_id: str, client: Optional[ClobClient] = None, market_id: Optional[str] = None) -> Dict:
    result = {
        'best_bid': 0.0, 'best_ask': 1.0,
        'best_bid_size': 0, 'best_bid_value': 0,
        'best_ask_size': 0, 'best_ask_value': 0,
        'tick_size': 0.0
    }

    if not client:
        return result

    try:
        tick_size = float(client.get_tick_size(token_id))
        orderbook = client.get_order_book(token_id)

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

    except:
        return result


# ============= 数据处理 =============

def extract_market_stats(market_data: Dict, market_config: Dict, client: Optional[ClobClient] = None) -> Dict:
    if not market_data:
        return {'name': market_config.get('name', 'Unknown'), 'enabled': market_config.get('enabled', False), 'error': True}

    trade_side = market_config.get('trade_side', 'no')
    token_id = market_config.get('yes_token_id') if trade_side == 'yes' else market_config.get('no_token_id')
    market_id = market_config.get('market_id')

    orderbook_depth = {'best_bid': 0.0, 'best_ask': 1.0, 'best_bid_value': 0, 'best_ask_value': 0}
    if token_id:
        orderbook_depth = get_orderbook_depth(token_id, client, market_id)

    target_value = float(market_config.get('max_position_value', 25))
    best_bid = orderbook_depth.get('best_bid', 0)
    best_ask = orderbook_depth.get('best_ask', 0)

    bid_levels = orderbook_depth.get('bid_levels', [])
    ask_levels = orderbook_depth.get('ask_levels', [])

    target_buy_price = find_price_by_value(bid_levels, target_value, is_bid=True)
    target_sell_price = find_price_by_value(ask_levels, target_value, is_bid=False)

    buy_cum_value = cumulative_value_to_price(bid_levels, target_buy_price, is_bid=True)
    sell_cum_value = cumulative_value_to_price(ask_levels, target_sell_price, is_bid=False)

    inside_buy_value = buy_cum_value if bid_levels else 0
    inside_sell_value = sell_cum_value if ask_levels else 0

    volume_24h = market_data.get('volume24hr', 0)
    volume_1w = market_data.get('volume1wk', 0)
    volume_6d = max(0, volume_1w - volume_24h)  # 前6天的量
    daily_avg_6d = volume_6d / 6  # 前6天日均量
    weighted_daily = daily_avg_6d * 0.7 + volume_24h * 0.3  # 加权日均量
    spread = target_sell_price - target_buy_price if target_sell_price > target_buy_price else 0

    base_value = inside_sell_value if inside_sell_value > 0 else orderbook_depth.get('best_ask_value', 0)
    turnover_ratio = weighted_daily / (base_value + target_value) * target_sell_price if base_value > 0 else 0
    yield_rate = spread * turnover_ratio

    return {
        'market_id': market_config.get('market_id'),
        'name': market_config.get('name', market_data.get('question', 'Unknown')),
        'enabled': market_config.get('enabled', False),
        'trade_side': market_config.get('trade_side', 'N/A'),
        'max_position_value': market_config.get('max_position_value', 25.0),
        'volume_24h': volume_24h,
        'volume_1w': volume_1w,
        'liquidity': market_data.get('liquidityNum', 0),
        'best_bid': best_bid,
        'best_ask': best_ask,
        'best_bid_value': inside_buy_value,
        'best_ask_value': inside_sell_value,
        'spread': spread,
        'turnover_ratio': turnover_ratio,
        'yield_rate': yield_rate,
        'active': market_data.get('active', False),
        'closed': market_data.get('closed', True),
        'error': False
    }


# ============= HTML 生成 =============

def save_to_html(markets_stats: List[Dict], history: Dict = None):
    valid_stats = [s for s in markets_stats if not s.get('error')]
    if not valid_stats:
        return

    # 计算7天平均收益率
    if history:
        for stat in valid_stats:
            market_id = stat.get('market_id')
            if market_id:
                stat['avg_yield_7d'] = calculate_avg_yield(market_id, history, days=7)

    # 排序
    def sort_key(x):
        avg = x.get('avg_yield_7d')
        if avg is None:
            return (1, -x.get('yield_rate', 0))
        return (0, -avg)

    sorted_stats = sorted(valid_stats, key=sort_key)
    enabled_markets = [m for m in valid_stats if m.get('enabled')]

    # 采样统计
    total_samples = 0
    earliest_ts = latest_ts = None
    if history:
        for records in history.values():
            total_samples += len(records)
            for r in records:
                ts = r.get('ts')
                if ts:
                    if earliest_ts is None or ts < earliest_ts:
                        earliest_ts = ts
                    if latest_ts is None or ts > latest_ts:
                        latest_ts = ts

    if earliest_ts and latest_ts:
        try:
            earliest_dt = datetime.fromisoformat(earliest_ts)
            latest_dt = datetime.fromisoformat(latest_ts)
            coverage_days = (latest_dt - earliest_dt).total_seconds() / 86400
            coverage_str = f"{coverage_days:.1f} 天"
            earliest_str = earliest_dt.strftime('%m-%d %H:%M')
            latest_str = latest_dt.strftime('%m-%d %H:%M')
        except:
            coverage_str = earliest_str = latest_str = "-"
    else:
        coverage_str = earliest_str = latest_str = "-"

    sample_count = total_samples // len(history) if history and len(history) > 0 else 0

    # 生成HTML
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📊</text></svg>">
    <title>Polymarket 市场数据</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 20px; min-height: 100vh; }}
        .container {{ max-width: 1400px; margin: 0 auto; background: white; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); overflow: hidden; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; }}
        .header h1 {{ font-size: 32px; margin-bottom: 10px; }}
        .header p {{ font-size: 16px; opacity: 0.9; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; padding: 30px; background: #f8f9fa; }}
        .stat-card {{ background: white; padding: 20px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); text-align: center; }}
        .stat-card h3 {{ font-size: 14px; color: #6c757d; margin-bottom: 8px; }}
        .stat-card p {{ font-size: 24px; font-weight: bold; color: #495057; }}
        .table-container {{ padding: 30px; overflow-x: auto; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
        thead {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; }}
        th {{ padding: 12px 8px; text-align: left; font-weight: 600; white-space: nowrap; }}
        th.right {{ text-align: right; }}
        tbody tr {{ border-bottom: 1px solid #e9ecef; }}
        tbody tr:hover {{ background-color: #f8f9fa; }}
        td {{ padding: 12px 8px; white-space: nowrap; }}
        td.right {{ text-align: right; }}
        .status {{ display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 600; }}
        .status.enabled {{ background: #d4edda; color: #155724; }}
        .status.paused {{ background: #fff3cd; color: #856404; }}
        .status.closed {{ background: #f8d7da; color: #721c24; }}
        .trade-side {{ display: inline-block; padding: 4px 8px; border-radius: 6px; font-size: 12px; font-weight: 600; }}
        .trade-side.yes {{ background: #d4edda; color: #155724; }}
        .trade-side.no {{ background: #f8d7da; color: #721c24; }}
        .footer {{ padding: 30px; color: #6c757d; background: #f8f9fa; }}
        .footer .source {{ text-align: center; margin-bottom: 20px; }}
        .formula {{ background: white; padding: 20px; border-radius: 8px; margin-top: 15px; font-size: 13px; text-align: left; }}
        .formula h4 {{ margin: 0 0 10px 0; color: #495057; }}
        .formula code {{ background: #e9ecef; padding: 2px 6px; border-radius: 4px; font-family: monospace; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Polymarket 市场数据概览</h1>
            <p>更新时间: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}</p>
        </div>
        <div class="stats-grid">
            <div class="stat-card"><h3>监控市场</h3><p>{len(valid_stats)} / {len(enabled_markets)} 启用</p></div>
            <div class="stat-card"><h3>采样次数</h3><p>{sample_count}</p></div>
            <div class="stat-card"><h3>数据覆盖</h3><p>{coverage_str}</p></div>
            <div class="stat-card"><h3>最早采样</h3><p>{earliest_str}</p></div>
            <div class="stat-card"><h3>最新采样</h3><p>{latest_str}</p></div>
        </div>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th>状态</th><th>市场名称</th><th>方向</th><th class="right">上限</th>
                        <th class="right">24h量</th><th class="right">7d量</th><th class="right">流动性</th>
                        <th class="right">买价</th><th class="right">买额</th><th class="right">卖价</th><th class="right">卖额</th>
                        <th class="right">价差</th><th class="right">周转</th><th class="right">收益率</th><th class="right">7d平均</th>
                    </tr>
                </thead>
                <tbody>
"""

    for stat in sorted_stats:
        if stat.get('closed'):
            status_class, status_text = 'closed', '已关闭'
        elif not stat.get('active'):
            status_class, status_text = 'paused', '未激活'
        elif stat.get('enabled'):
            status_class, status_text = 'enabled', '已启用'
        else:
            status_class, status_text = 'paused', '已暂停'

        trade_side = stat['trade_side'].upper()
        side_class = 'yes' if trade_side == 'YES' else 'no'

        turnover = stat.get('turnover_ratio', 0)
        turnover_str = f"{turnover:.1f}x" if turnover >= 1 else (f"{turnover:.2f}x" if turnover > 0 else "-")

        best_bid_str = f"{stat['best_bid']*100:.1f}%" if stat['best_bid'] else '-'
        best_ask_str = f"{stat['best_ask']*100:.1f}%" if stat['best_ask'] else '-'
        spread_str = f"{stat.get('spread', 0)*100:.1f}%" if stat.get('spread', 0) > 0 else '-'

        yield_rate = stat.get('yield_rate', 0)
        yield_str = f"{yield_rate:.1f}" if yield_rate >= 1 else (f"{yield_rate:.2f}" if yield_rate > 0 else "-")

        avg_yield_7d = stat.get('avg_yield_7d')
        avg_str = "-" if avg_yield_7d is None else (f"{avg_yield_7d:.1f}" if avg_yield_7d >= 1 else f"{avg_yield_7d:.2f}" if avg_yield_7d > 0 else "-")

        html += f"""<tr>
            <td><span class="status {status_class}">{status_text}</span></td>
            <td>{stat['name']}</td>
            <td><span class="trade-side {side_class}">{trade_side}</span></td>
            <td class="right">${stat.get('max_position_value', 25):.0f}</td>
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
            <td class="right">{avg_str}</td>
        </tr>
"""

    html += """</tbody></table></div>
        <div class="footer">
            <p class="source">数据来源: Polymarket API</p>
            <div class="formula">
                <h4>公式说明</h4>
                <p><strong>加权日均量</strong> = <code>前6天日均量 × 0.7 + 24小时量 × 0.3</code></p>
                <p><strong>周转</strong> = <code>加权日均量 / (卖单流动性 + 目标持仓) × 卖价</code></p>
                <p><strong>收益率</strong> = <code>价差 × 周转</code></p>
            </div>
        </div>
    </div>
</body>
</html>"""

    with open(HTML_FILE, 'w', encoding='utf-8') as f:
        f.write(html)


def sync_to_github():
    """将 HTML 文件同步到 GitHub"""
    try:
        result = subprocess.run(
            ['git', 'diff', '--quiet', 'polymarket_markets.html'],
            cwd=SCRIPT_DIR,
            capture_output=True
        )
        if result.returncode == 0:
            return False

        subprocess.run(
            ['git', 'add', 'polymarket_markets.html'],
            cwd=SCRIPT_DIR,
            check=True,
            capture_output=True
        )

        commit_msg = f"Update market data {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        subprocess.run(
            ['git', 'commit', '-m', commit_msg],
            cwd=SCRIPT_DIR,
            check=True,
            capture_output=True
        )

        subprocess.run(
            ['git', 'push'],
            cwd=SCRIPT_DIR,
            check=True,
            capture_output=True
        )
        return True
    except subprocess.CalledProcessError as e:
        console.print(f"[yellow]⚠ Git同步失败: {e}[/yellow]")
        return False
    except Exception as e:
        console.print(f"[yellow]⚠ Git同步异常: {e}[/yellow]")
        return False


# ============= 主循环 =============

def run_cycle(client: Optional[ClobClient] = None):
    """执行一次采样和报告生成"""
    console.print(f"\n[bold cyan]{'=' * 50}[/bold cyan]")
    console.print(f"[bold cyan]更新 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/bold cyan]")
    console.print(f"[bold cyan]{'=' * 50}[/bold cyan]")

    markets_config = load_markets_config()
    history = load_history()
    total_markets = len(markets_config)

    console.print(f"[dim]市场: {total_markets} | 历史记录: {len(history)}[/dim]")

    # 获取市场数据
    markets_stats = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]获取市场数据...", total=total_markets)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(get_market_data, c['market_id']): c for c in markets_config}
            for future in as_completed(futures):
                config = futures[future]
                market_id = config['market_id']
                try:
                    market_data = future.result()
                    if market_data:
                        stat = extract_market_stats(market_data, config, client)
                        markets_stats.append(stat)
                        # 保存收益率到历史
                        if not stat.get('error'):
                            if market_id not in history:
                                history[market_id] = []
                            history[market_id].append({
                                'ts': datetime.now().isoformat(),
                                'yield_rate': round(stat.get('yield_rate', 0), 4)
                            })
                except:
                    pass
                progress.update(task, advance=1)

    console.print(f"[green]✓ 完成: {len(markets_stats)}/{total_markets}[/green]")

    # 备份和保存
    history = backup_old_data(history, days=7)
    save_history(history)

    # 生成HTML
    save_to_html(markets_stats, history)
    console.print(f"[green]✓ HTML已更新: {HTML_FILE}[/green]")

    # 同步到GitHub
    if sync_to_github():
        console.print("[green]✓ 已同步到GitHub[/green]")


def main():
    parser = argparse.ArgumentParser(description="Polymarket 市场监控")
    parser.add_argument("-i", "--interval", type=int, default=3600, help="更新间隔（秒），默认3600")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    console.print("\n[bold green]" + "=" * 50 + "[/bold green]")
    console.print("[bold green]Polymarket 市场监控服务[/bold green]")
    console.print("[bold green]" + "=" * 50 + "[/bold green]")
    console.print(f"启动: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    console.print(f"间隔: {args.interval} 秒 ({args.interval/60:.1f} 分钟)")
    console.print("按 Ctrl+C 停止")
    console.print("[bold green]" + "=" * 50 + "[/bold green]")

    # 初始化客户端
    client = None
    if PK and PROXY_ADDRESS:
        try:
            client = ClobClient(HOST, key=PK, chain_id=CHAIN_ID, signature_type=2, funder=PROXY_ADDRESS)
            client.set_api_creds(client.create_or_derive_api_creds())
            console.print("[green]✓ 客户端初始化成功[/green]")
        except Exception as e:
            console.print(f"[yellow]⚠ 客户端初始化失败: {e}[/yellow]")

    round_count = 0
    while running:
        round_count += 1
        try:
            run_cycle(client)
        except Exception as e:
            console.print(f"[red]错误: {e}[/red]")

        if not running:
            break

        console.print(f"\n[dim]等待 {args.interval} 秒...[/dim]")
        for _ in range(args.interval):
            if not running:
                break
            time.sleep(1)

    console.print(f"\n[yellow]已停止，共运行 {round_count} 轮[/yellow]")


if __name__ == "__main__":
    main()
