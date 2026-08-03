#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
妖币系统 VIP 到期提醒
扫描所有 VIP 用户，找出即将到期（默认 3 天内）的账号。
输出到 stdout，便于 OpenClaw cron 任务捕获后推送。
"""
import json
import time
import sys
import os

BASE = os.path.dirname(os.path.abspath(__file__))
USERS = os.path.join(BASE, "data", "users.json")
# 提前提醒天数，可用环境变量覆盖
AHEAD_DAYS = int(os.getenv("VIP_AHEAD_DAYS", "3"))

def main():
    if not os.path.exists(USERS):
        print("无用户数据文件")
        return
    data = json.load(open(USERS, encoding="utf-8"))
    now = time.time()
    expiring = []
    for name, u in data.items():
        exp = (u or {}).get("vip_expiry", "")
        if not exp or not u.get("is_vip"):
            continue
        try:
            t = time.mktime(time.strptime(exp, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            continue
        remain_h = (t - now) / 3600
        if 0 <= remain_h <= AHEAD_DAYS * 24:
            expiring.append((name, exp, remain_h))

    if not expiring:
        return

    lines = [f"⚠️ 妖币系统 VIP 到期提醒（{time.strftime('%Y-%m-%d %H:%M')}）"]
    for name, exp, h in expiring:
        if h <= 24:
            flag = "🔴 今天到期"
        elif h <= 72:
            flag = "🟠 3天内"
        else:
            flag = "🟡 即将"
        lines.append(f"  {name}: {flag} 到期时间 {exp}（剩 {h/24:.1f} 天）")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
