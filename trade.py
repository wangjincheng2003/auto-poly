#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket多市场自动交易脚本
策略：低买高卖，赚取0.5%以上的差价
"""

import time
import os
import json
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OpenOrderParams, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL
from notify import send_wechat

load_dotenv()

# 创建带连接池和重试的Session
def create_session():
    s = requests.Session()
    retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=50, pool_maxsize=50)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

session = create_session()

# ============= 配置 =============

class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    GRAY = '\033[90m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'

HOST = "https://clob.polymarket.com"
PK = os.getenv("PK")
CHAIN_ID = 137
PROXY_ADDRESS = os.getenv("PROXY_ADDRESS")

MIN_PROFIT = 0.007
MIN_ORDER_VALUE = 5.0
SCAN_INTERVAL = 10

# 持仓追踪（用于检测成交）
last_sizes = {}  # {market_id: size}

# ============= 工具函数 =============

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), 'markets_config.json')
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def normalize_price(price, tick_size):
    return round(price / tick_size) * tick_size

def get_order_remaining_size(order):
    return float(order['original_size']) - float(order['size_matched'])

def get_my_sizes_by_price(orders, tick_size):
    """计算每个价格上自己的订单总量"""
    sizes = {}
    for order in orders:
        price = normalize_price(float(order['price']), tick_size)
        sizes[price] = sizes.get(price, 0) + get_order_remaining_size(order)
    return sizes

def aggregate_other_liquidity(orderbook_side, my_sizes, tick_size, descending=True):
    """汇总除自己以外的每个价格档的数量，并排序"""
    aggregated = {}
    for level in orderbook_side:
        price = normalize_price(float(level.price), tick_size)
        other_size = float(level.size) - my_sizes.get(price, 0)
        if other_size <= 0:
            continue
        aggregated[price] = aggregated.get(price, 0) + other_size

    sorted_prices = sorted(aggregated.keys(), reverse=descending)
    return [(p, aggregated[p]) for p in sorted_prices]

def find_price_by_value(levels, target_value, is_bid=True):
    """
    从最优价开始累加其他人的挂单金额，找到累计金额>=目标金额的价格档
    levels: [(price, other_size)] 已按合理方向排序（买: 高->低，卖: 低->高）
    """
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

def get_portfolio_summary(client):
    """获取完整的portfolio（持仓+现金）"""
    try:
        # 获取所有持仓和余额
        positions = session.get("https://data-api.polymarket.com/positions",
                               params={'user': PROXY_ADDRESS}, timeout=10).json()
        usdc_balance = float(client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))['balance']) / 1e6 # type: ignore

        # 过滤有效持仓并格式化
        valid_positions = [(float(p['size']), float(p['currentValue']),
                           p.get('market', p.get('asset_id', ''))[:30])
                          for p in positions if float(p['size']) > 0.01]

        lines = [f"- {market}: {size:.2f} (${value:.2f})" for size, value, market in valid_positions]
        lines.append(f"- 现金(USDC): ${usdc_balance:.2f}")

        # 计算总价值
        total = sum(value for _, value, _ in valid_positions) + usdc_balance
        lines.append(f"\n**总计**: ${total:.2f}")

        return "\n".join(lines)
    except Exception as e:
        return f"获取失败: {str(e)[:50]}"

def manage_orders_smart(client, orders, target_price, target_value, side, token_id, tick_size):
    """智能管理订单：只调整差额"""
    target_price = normalize_price(target_price, tick_size)

    # 取消价格不对的订单
    for order in orders:
        if normalize_price(float(order['price']), tick_size) != target_price:
            client.cancel(order['id'])
            print(f"{Colors.RED}取消{'买单' if side == BUY else '卖单'}: 价格错误 {float(order['price']):.3f}{Colors.RESET}")

    # 获取价格正确的订单（按创建时间倒序）
    correct_orders = [o for o in orders if normalize_price(float(o['price']), tick_size) == target_price]
    correct_orders.sort(key=lambda x: x['created_at'], reverse=True)

    # 计算当前订单总值
    current_value = sum(get_order_remaining_size(o) * target_price for o in correct_orders)

    # 删除多余的订单
    cancelled = 0
    if current_value > target_value + 0.01:
        for order in correct_orders:
            if current_value <= target_value + 0.01:
                break
            order_value = get_order_remaining_size(order) * target_price
            client.cancel(order['id'])
            current_value -= order_value
            cancelled += 1
            print(f"{Colors.RED}取消{'买单' if side == BUY else '卖单'}: 金额多余 ${order_value:.2f}{Colors.RESET}")

    # 补充差额订单
    added = 0
    shortage = target_value - current_value
    if shortage >= MIN_ORDER_VALUE:
        if side == BUY:
            # 买单：拆分成多个不超过10 USDC的订单
            max_order_value = 10.0
            while shortage >= MIN_ORDER_VALUE:
                order_value = min(shortage, max_order_value)
                size = order_value / target_price
                client.create_and_post_order(OrderArgs(
                    price=target_price, size=size, side=side, token_id=token_id))
                added += 1
                print(f"{Colors.GREEN}创建买单: 价格={target_price:.3f}, 数量={size:.2f}, 金额=${order_value:.2f}{Colors.RESET}")
                shortage -= order_value
        else:
            # 卖单：一个订单
            size = shortage / target_price
            client.create_and_post_order(OrderArgs(
                price=target_price, size=size, side=side, token_id=token_id))
            added = 1
            print(f"{Colors.GREEN}创建卖单: 价格={target_price:.3f}, 数量={size:.2f}, 金额=${shortage:.2f}{Colors.RESET}")

    return len(correct_orders) - cancelled + added

# ============= 市场处理 =============

def process_market(client, market_config):
    market_name = market_config['name']
    market_id = market_config['market_id']
    trade_side = market_config['trade_side']
    token_id = market_config['yes_token_id'] if trade_side == 'yes' else market_config['no_token_id']
    max_position = market_config['max_position_value']

    # 获取订单和订单簿
    active_orders = client.get_orders(OpenOrderParams(market=market_id))
    buy_orders = [o for o in active_orders if o['side'] == 'BUY']
    sell_orders = [o for o in active_orders if o['side'] == 'SELL']
    orderbook = client.get_order_book(token_id)
    tick_size = float(client.get_tick_size(token_id))

    # 计算除自己以外的真实市场价格与档位
    my_buy_sizes = get_my_sizes_by_price(buy_orders, tick_size)
    my_sell_sizes = get_my_sizes_by_price(sell_orders, tick_size)
    bid_levels = aggregate_other_liquidity(orderbook.bids, my_buy_sizes, tick_size, descending=True)
    ask_levels = aggregate_other_liquidity(orderbook.asks, my_sell_sizes, tick_size, descending=False)
    best_bid = bid_levels[0][0] if bid_levels else 0.0
    best_ask = ask_levels[0][0] if ask_levels else 1.0

    # 获取持仓
    response = session.get("https://data-api.polymarket.com/positions",
                           params={'user': PROXY_ADDRESS, 'market': market_id},
                           timeout=10)
    positions = response.json()

    if positions:
        current_size = float(positions[0]['size'])
        avg_buy_price = float(positions[0]['avgPrice'])
        current_position = float(positions[0]['currentValue'])
    else:
        current_position = current_size = avg_buy_price = 0.0

    # 获取余额
    balance_info = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))  # type: ignore
    usdc_balance = float(balance_info['balance']) / 1e6
    cost_basis = current_size * avg_buy_price  # 使用初始下注金额而非当前市值
    available_position = max(0, min(max_position - cost_basis, usdc_balance))
    # 计算下单价格（按其他人订单累计金额确定档位）
    if bid_levels and ask_levels:
        buy_price = find_price_by_value(bid_levels, available_position, is_bid=True)
        # 只有价差足够才挂买单
        target_buy_value = available_position if (best_ask - buy_price) >= MIN_PROFIT else 0
    else:
        buy_price = best_bid
        target_buy_value = 0

    if current_size > 0:
        target_profit_price = normalize_price(min(avg_buy_price + MIN_PROFIT, 0.999), tick_size)
        if ask_levels:
            sell_price = max(find_price_by_value(ask_levels, current_size * best_ask, is_bid=False),
                             target_profit_price)
        else:
            sell_price = target_profit_price
        sell_price = min(sell_price, 0.999)
        target_sell_value = current_size * sell_price
    else:
        sell_price = best_ask
        target_sell_value = 0

    buy_count = manage_orders_smart(client, buy_orders, buy_price, target_buy_value, BUY, token_id, tick_size)
    sell_count = manage_orders_smart(client, sell_orders, sell_price, target_sell_value, SELL, token_id, tick_size)

    # 检测持仓数量变化并推送微信通知
    if market_id in last_sizes:  # 不是首次运行
        last_size = last_sizes[market_id]
        change = current_size - last_size
        if abs(change) > 0.01:  # 变化超过0.01才推送
            portfolio = get_portfolio_summary(client)
            send_wechat(
                f"{'🟢 买入成交' if change > 0 else '🔴 卖出成交'} - {market_name}",
                f"**市场**: {market_name}\n\n**数量变化**: {change:+.2f}\n\n**当前持仓**: {current_size:.2f} (${current_position:.2f})\n\n**完整Portfolio**:\n{portfolio}"
            )
    last_sizes[market_id] = current_size

    return {
        'name': market_name,
        'side': trade_side,
        'best_bid': best_bid,
        'best_ask': best_ask,
        'buy_price': buy_price,
        'sell_price': sell_price,
        'tick_size': tick_size,
        'position_value': current_position,
        'max_position_value': max_position,
        'position_ratio': current_position / max_position if max_position > 0 else 0,
        'buy_orders_count': buy_count,
        'sell_orders_count': sell_count,
    }

# ============= 主程序 =============

print("=" * 50)
print("检查环境变量配置...")
if not PK or not PROXY_ADDRESS:
    print("❌ 错误：缺少必要的环境变量！")
    exit(1)
print("✓ 环境变量检查通过！")

print("初始化Polymarket客户端...")
client = ClobClient(HOST, key=PK, chain_id=CHAIN_ID, signature_type=2, funder=PROXY_ADDRESS)
client.set_api_creds(client.create_or_derive_api_creds())
print("✓ 客户端初始化成功！")
print("=" * 50)

# 发送启动通知
send_wechat(
    "🚀 交易脚本已启动",
    f"**启动时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n**钱包**: {PROXY_ADDRESS[:10]}...{PROXY_ADDRESS[-8:]}\n\n**扫描间隔**: {SCAN_INTERVAL}秒"
)

round_count = 0
consecutive_errors = 0  # 连续失败计数器
while True:
    try:
        round_count += 1
        print(f"\n{Colors.BLUE}{'=' * 60}{Colors.RESET}")
        print(f"{Colors.BLUE}[{time.strftime('%Y-%m-%d %H:%M:%S')}] 第 {round_count} 轮扫描{Colors.RESET}")
        print(f"{Colors.BLUE}{'=' * 60}{Colors.RESET}\n")

        config = load_config()
        markets = [m for m in config['markets'] if m.get('enabled')]

        # 并发处理所有市场
        with ThreadPoolExecutor(max_workers=len(markets)) as executor:
            stats = list(executor.map(lambda m: process_market(client, m), markets))

        # 输出结果
        for stat in stats:
            # 格式化输出
            price_fmt = '.3f' if stat['tick_size'] == 0.001 else '.2f'
            spread = stat['sell_price'] - stat['buy_price']
            spread_pct = (spread / stat['buy_price'] * 100) if stat['buy_price'] > 0 else 0
            ratio = stat['position_ratio'] * 100

            pos_color = Colors.GREEN if stat['position_value'] > 0 else Colors.GRAY
            buy_color = Colors.GREEN if stat['buy_orders_count'] > 0 else Colors.GRAY
            sell_color = Colors.GREEN if stat['sell_orders_count'] > 0 else Colors.GRAY

            print(f"{Colors.YELLOW}[{stat['name']}] [{stat['side'].upper()}]{Colors.RESET}")
            print(f"  {Colors.GRAY}市场: 买@{stat['best_bid']:{price_fmt}} 卖@{stat['best_ask']:{price_fmt}}{Colors.RESET}")
            print(f"  {Colors.GRAY}下单: 买@{stat['buy_price']:{price_fmt}} 卖@{stat['sell_price']:{price_fmt}} | 赚={spread:{price_fmt}}({spread_pct:.2f}%){Colors.RESET}")
            print(f"  {pos_color}持仓={stat['position_value']:.1f}/{stat['max_position_value']:.0f}({ratio:.0f}%){Colors.RESET} {Colors.GRAY}|{Colors.RESET} {buy_color}买单{stat['buy_orders_count']}个{Colors.RESET} {sell_color}卖单{stat['sell_orders_count']}个{Colors.RESET}")
            print()

        # 成功执行，重置失败计数
        consecutive_errors = 0

        print(f"\n{'─' * 60}")

    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断，程序退出")
        send_wechat(
            "⏹️ 交易脚本已停止",
            f"**停止时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n**运行轮次**: {round_count}轮\n\n**原因**: 用户手动中断"
        )
        break

    except Exception as e:
        consecutive_errors += 1
        print(f"\n{Colors.RED}❌ 第 {round_count} 轮出错: {type(e).__name__}: {str(e)[:1000]}{Colors.RESET}")

        # 如果是网络连接异常，重置Session
        if "Request exception" in str(e) or "Connection" in str(e):
            print(f"{Colors.YELLOW}🔄 检测到连接异常，重置Session...{Colors.RESET}")
            try:
                session.close()
            except:
                pass
            session = create_session()

        # 连续失败50次发送告警
        if consecutive_errors == 50:
            send_wechat(
                "⚠️ 连续失败告警",
                f"**告警时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n**连续失败**: {consecutive_errors}轮\n\n**最近错误**: {type(e).__name__}: {str(e)[:200]}"
            )

        print(f"{Colors.YELLOW}⏳ 等待 {SCAN_INTERVAL} 秒后继续下一轮...{Colors.RESET}\n")
        time.sleep(SCAN_INTERVAL)
        continue

    print(f"\n⏳ 等待 {SCAN_INTERVAL} 秒后进行下一轮扫描...")
    time.sleep(SCAN_INTERVAL)
