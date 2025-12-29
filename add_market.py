#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动添加Polymarket市场到配置文件的脚本
用法: python add_market.py <market-slug>
"""

import sys
import json
import os
import requests
from pprint import pprint
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OpenOrderParams, BalanceAllowanceParams, AssetType

load_dotenv()
# API配置
HOST = "https://clob.polymarket.com"
PK = os.getenv("PK")
CHAIN_ID = 137  # Polygon链ID
# 配置文件路径
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'markets_config.json')
API_BASE_URL = "https://gamma-api.polymarket.com/markets/slug/"

def fetch_market_info(slug):
    """从Polymarket API获取市场信息"""
    url = f"{API_BASE_URL}{slug}"
    print(f"🔍 正在获取市场信息: {url}")

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"❌ 获取市场信息失败: {e}")
        return None

def parse_market_data(data):
    """解析市场数据，提取需要的字段"""
    try:
        # 提取基本信息
        market_name = data['question']
        market_id = data['conditionId']

        # clobTokenIds是一个数组[Yes_token, No_token]
        clob_tokens = json.loads(data['clobTokenIds'])
        yes_token_id = clob_tokens[0]  # Yes token
        no_token_id = clob_tokens[1]   # No token
        
        # 提取市场参数
        tick_size = float(data.get('orderPriceMinTickSize', 0.01))
        min_size = float(data.get('orderMinSize', 5))

        # 市场状态
        active = data.get('active', True)
        accepting_orders = data.get('acceptingOrders', True)

        return {
            'name': market_name,
            'market_id': market_id,
            'yes_token_id': yes_token_id,
            'no_token_id': no_token_id,
            'tick_size': tick_size,
            'min_size': min_size,
            'active': active and accepting_orders,
            'slug': data.get('slug', '')
        }
    except (KeyError, json.JSONDecodeError) as e:
        print(f"❌ 解析市场数据失败: {e}")
        return None

def load_config():
    """加载现有配置"""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"❌ 配置文件不存在: {CONFIG_PATH}")
        return None
    except json.JSONDecodeError as e:
        print(f"❌ 配置文件格式错误: {e}")
        return None

def save_config(config):
    """保存配置到文件"""
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"❌ 保存配置文件失败: {e}")
        return False

def check_market_exists(config, market_id):
    """检查市场是否已存在"""
    for market in config['markets']:
        if market['market_id'] == market_id:
            return True
    return False

def add_market(slug, max_position=25.0):
    """
    添加市场到配置文件

    参数:
        slug: 市场的slug标识符
        max_position: 最大持仓价值（默认50 USDC）
    """
    
    # 1. 获取市场信息
    market_data = fetch_market_info(slug)
    if not market_data:
        return False

    # 2. 解析市场数据
    parsed = parse_market_data(market_data)
    if not parsed:
        return False

    print(f"\n📊 市场信息:")
    print(f"   名称: {parsed['name']}")
    print(f"   Market ID: {parsed['market_id'][:20]}...")
    print(f"   Yes Token ID: {parsed['yes_token_id'][:20]}...")
    print(f"   No Token ID: {parsed['no_token_id'][:20]}...")
    print(f"   Tick Size: {parsed['tick_size']}")
    print(f"   Min Size: {parsed['min_size']}")
    print(f"   状态: {'✅ 活跃' if parsed['active'] else '⚠️  暂停'}")

    # 3. 加载现有配置
    config = load_config()
    if not config:
        return False

    # 4. 检查是否已存在
    if check_market_exists(config, parsed['market_id']):
        print(f"\n⚠️  市场已存在于配置文件中: {parsed['name']}")

        # 询问是否要更新
        response = input("是否要更新该市场配置? (y/n): ").lower()
        if response != 'y':
            print("❌ 取消操作")
            return False

        # 删除旧配置
        config['markets'] = [m for m in config['markets'] if m['market_id'] != parsed['market_id']]
        print("✓ 已删除旧配置")

    # 5. 询问配置参数
    print(f"\n⚙️  配置参数:")

    try:
        # 选择交易方向
        print("\n交易方向:")
        print("  YES - 交易Yes代币（认为会发生）")
        print("  NO  - 交易No代币（认为不会发生）")
        side_input = input("选择交易方向 (yes/NO, 默认NO): ").strip().lower()
        trade_side = 'yes' if side_input == 'yes' else 'no'

        max_pos_input = input(f"\n最大持仓价值 (默认{max_position} USDC, 回车跳过): ").strip()
        if max_pos_input:
            max_position = float(max_pos_input)

        enabled_input = input("是否立即启用? (Y/n, 默认Y): ").strip().lower()
        enabled = enabled_input != 'n'

    except ValueError as e:
        print(f"❌ 输入错误: {e}")
        return False

    # 6. 创建新市场配置
    new_market = {
        "enabled": enabled,
        "name": parsed['name'],
        "market_id": parsed['market_id'],
        "yes_token_id": parsed['yes_token_id'],
        "no_token_id": parsed['no_token_id'],
        "trade_side": trade_side,
        "max_position_value": max_position
    }

    # 7. 添加到配置
    config['markets'].append(new_market)

    # 8. 保存配置
    if not save_config(config):
        return False

    print(f"\n✅ 成功添加市场到配置文件!")
    print(f"   配置文件: {CONFIG_PATH}")
    print(f"   市场数量: {len(config['markets'])}")
    print(f"   交易方向: {trade_side.upper()}")
    print(f"   状态: {'🟢 已启用' if enabled else '🔴 未启用'}")

    return True


"""主函数"""
print("=" * 60)
print("Polymarket 市场添加工具")
print("=" * 60)
client2 = ClobClient(
    HOST,
    key=PK,
    chain_id=CHAIN_ID
)
client2.set_api_creds(client2.create_or_derive_api_creds())
    
# 检查命令行参数
if len(sys.argv) > 1:
    slug = sys.argv[1]
else:
    # 交互式输入
    slug = input("\n请输入市场slug (例如: will-jia-yueting-enter-mainland-china-by): ").strip()

if not slug:
    print("❌ 错误: 市场slug不能为空")
    print("\n用法: python add_market.py <market-slug>")
    sys.exit(1)

# 移除可能的完整URL前缀
if slug.startswith('http'):
    slug = slug.split('/')[-1]

print(f"\n📍 市场Slug: {slug}\n")

# 添加市场
success = add_market(slug)

if success:
    print("\n🎉 完成! 现在可以运行 simple_trade.py 开始交易")
else:
    print("\n❌ 添加市场失败")
    sys.exit(1)
