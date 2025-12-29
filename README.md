# Auto-Poly

Polymarket 自动做市交易机器人，通过低买高卖策略赚取价差收益。

## 功能特点

- **自动做市**: 在多个市场同时挂单，低买高卖赚取 0.7%+ 的价差
- **智能订单管理**: 自动调整订单价格和数量，避免重复挂单
- **持仓追踪**: 实时监控持仓变化，成交后自动推送微信通知
- **多市场并发**: 支持同时监控多个预测市场
- **风险控制**: 每个市场设置最大持仓限制

## 项目结构

```
auto-poly/
├── trade.py            # 主交易脚本
├── add_market.py       # 添加新市场的工具
├── notify.py           # 微信推送通知（Server酱）
├── markets_config.json # 市场配置文件
└── .env                # 环境变量（私钥等敏感信息）
```

## 安装

1. 克隆仓库:
```bash
git clone https://github.com/wangjincheng2003/auto-poly.git
cd auto-poly
```

2. 安装依赖:
```bash
pip install py-clob-client python-dotenv requests
```

3. 配置环境变量，创建 `.env` 文件:
```env
PK=你的钱包私钥
PROXY_ADDRESS=你的Polymarket代理钱包地址
SCKEY=Server酱推送密钥（可选）
```

## 使用方法

### 添加市场

```bash
python add_market.py <market-slug>
```

例如:
```bash
python add_market.py will-china-invade-taiwan-in-2025
```

脚本会引导你选择交易方向（YES/NO）和最大持仓金额。

### 启动交易

```bash
python trade.py
```

脚本会：
1. 每10秒扫描一次所有启用的市场
2. 根据市场深度计算最优挂单价格
3. 自动管理买卖订单
4. 成交时推送微信通知

## 配置说明

`markets_config.json` 中每个市场的配置项：

| 字段 | 说明 |
|------|------|
| `enabled` | 是否启用该市场 |
| `name` | 市场名称 |
| `market_id` | 市场ID |
| `yes_token_id` | YES代币ID |
| `no_token_id` | NO代币ID |
| `trade_side` | 交易方向 (`yes` 或 `no`) |
| `max_position_value` | 最大持仓价值 (USDC) |

## 交易策略

1. **买入**: 在买一价附近挂单，累计金额达到可用仓位时确定价格
2. **卖出**: 确保卖出价格比买入均价高出 0.7% 以上
3. **订单拆分**: 买单自动拆分为多个 ≤10 USDC 的小单，提高成交概率
4. **价格跟随**: 自动取消价格偏离的订单，在新价格重新挂单

## 注意事项

- 需要 Polygon 链上的 USDC 作为交易资金
- 首次使用需要在 Polymarket 网站授权代理钱包
- 建议小资金测试后再增加仓位
- 私钥请妥善保管，不要泄露

## License

MIT
