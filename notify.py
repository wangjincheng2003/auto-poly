#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Server酱微信推送"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

SCKEY = os.getenv("SCKEY")


def send_wechat(title, content):
    """发送微信推送"""
    if not SCKEY:
        return False

    try:
        response = requests.get(
            f'https://sctapi.ftqq.com/{SCKEY}.send',
            params={'title': title, 'desp': content},
            timeout=10
        )
        return response.json().get('code') == 0
    except:
        return False


if __name__ == "__main__":
    # 测试
    if send_wechat("测试通知", "配置成功！"):
        print("✅ 测试成功")
    else:
        print("❌ 测试失败")
