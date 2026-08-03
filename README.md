# 妖币雷达交易系统 V2.0 — 后端重建版

基于对线上 `yang9527.dpdns.org` 抓取分析后，用 **Flask** 等价的完整重建。前端 `templates/index.html` 为**线上抓取的原版**。

## 管理后台

- 地址：`/yaob/admin`（或交易面板右上角「👑 后台」按钮）
- 管理员：`XJarvis`（可用 `ADMIN_USER` 环境变量改）
- 功能：查看所有用户、**开通 VIP（可设有效天数）**、延长/调整 VIP、撤销 VIP、删除账号
- 开通 VIP 可选有效期：输入天数（0 或留空 = 永久；如 30/90/365）。到期后自动失效（`_is_vip` 校验 `vip_expiry`）
- **套餐快捷授权**：每个用户操作区下方有「快捷」按钮：30天 / 90天 / 180天 / 365天，一键开通对应天数 VIP
- 非管理员访问管理接口返回 403；普通用户看不到后台入口

## VIP 到期自动提醒

- 脚本：`vip_expiry_check.py`（扫描 3 天内即将到期的 VIP，可在 `VIP_AHEAD_DAYS` 环境变量调提前天数）
- 已配置 OpenClaw cron 任务「妖币VIP到期提醒」：每天 09:00 运行，推送到 Telegram
- `XJarvis` 管理员为永久 VIP 不计入；无到期用户则不推送

## 启动

```bash
cd yang9527_rebuild
# 首次: python3 -m venv .venv && .venv/bin/pip install flask
bash run.sh
# 或: .venv/bin/python app.py
# 访问 http://<IP>:8100/
```

- 首个登录用户自动创建为 **VIP**（便于自测）
- 默认端口 8100，可用 `PORT` 环境变量改

## 域名部署（已完成）
已配置 Nginx 反代：** https://jarvis-wx.cloud/yaob/ **

- 页面：`/yaob/` → 本地 8100（前端绝对路径 `/api/*` 由 nginx `sub_filter` 重写为 `/yaob/api/*`，前端零改动）
- API：`/yaob/api/*` → 8080 `/api/*`
- 登录/注册/登出：`/yaob/login`、`/yaob/register`、`/yaob/logout`
- 后端以 `YAOB_BASE=/yaob` 启动（生成带前缀的重定向）

## 币安数据接入（已完成）
- 直连币安被地区限制，已通过本机 Mihomo 代理（`127.0.0.1:7890`）访问
- 已给 Mihomo 加规则：`binance.com` 等域名 → SG（新加坡）节点（美区 IP 被币安 451 拒）
- `fapi.py` 默认 `FAPI_PROXY=http://127.0.0.1:7890`，可用 `FAPI_PROXY` 环境变量覆盖
- 全市场 USDT 永续扫描约 1 秒，真实候选信号

## 已复刻的完整 API（与线下版一致）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/login` `/register` `/logout` |  | 用户/会话（Flask session） |
| `/api/dashboard` | GET | 主面板聚合 |
| `/api/stats` | GET | 统计(daily_pnl/days/trade_counts 新版schema) |
| `/api/control` | POST | 开仓保证金+杠杆 |
| `/api/set_api_keys` `/clear_api_keys` | POST | 币安密钥 |
| `/api/toggle_auto_trade` | POST | 程序开关 |
| `/api/toggle_margin_mode` | POST | 逐仓/全仓 |
| `/api/toggle_exclude_large_cap` | POST | 大市值忽略 |
| `/api/toggle_strategy` | POST | 策略开关(**全量需VIP**) |
| `/api/get/save_strategy_params` | GET/POST | 策略参数 |
| `/api/get_excluded_symbols_categorized` | GET | 黑名单(crypto/index) |
| `/api/add/remove/clear/restore_default_excluded` | POST | 黑名单管理 |
| `/api/test_alert` `/api/reset_stats` | POST | 告警/统计 |

## 相对"前端模板版"复刻到位的线上差异
1. 符号带 `/`（`ZEC/USDT`）
2. 策略切换**所有策略**都需 VIP（非仅 D/E/F）
3. stats 用 `daily_pnl/days/trade_counts` 新版 schema
4. 黑名单分 crypto + index 两类
5. Flask session 鉴权

## 说明
- **扫描引擎已接入币安 FAPI 真实行情**，走本机代理 `127.0.0.1:7890`（Mihomo/clash）。全市场 USDT 永续单轮扫描约 1 秒。
- 策略 A-F 全部基于 24h ticker 单次拉取实现（A 涨幅做空/B 当日涨幅做空/C 回撤做多/D 分钟涨幅做空[需 1m 线]/E 冲高回落做多/F 斐波那契双向）——文案与线上版一致（如"斐波那契做空(反弹至38.2%, 阻力...)"）。
- **真实下单/账户资产**需在页面"填充密钥"填币安 API Key+Secret（未填则只读行情、不下单，资产显示 0）。
- 代理可用 `FAPI_PROXY` 环境变量覆盖；扫描范围可用 `SCAN_SYMBOLS="ZECUSDT,AAVEUSDT"` 限定。
- 策略默认 **止损 -20%**（保守安全值，按需在页面调整）。
- 生产部署请换 `FLASK_SECRET_KEY`，并用 gunicorn 等 WSGI 服务器。
