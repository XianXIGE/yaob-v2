# -*- coding: utf-8 -*-
"""
妖币雷达交易系统 V2.0 — 后端重建
基于对 yang9527.dpdns.org 线上版抓取复刻的 Flask 实现
"""
import json, os, time, math, random, threading
from pathlib import Path
from flask import (Flask, request, session, redirect, url_for,
                   render_template, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
# 生产环境请用环境变量覆盖
app.secret_key = os.getenv("FLASK_SECRET_KEY", "yaob-2.0-secret-key-please-change")

# 子路径部署前缀（nginx 反代到 /yaob/ 时设为 /yaob）
YAOB_BASE = os.getenv("YAOB_BASE", "").rstrip("/")


def _url(path):
    """生成带子路径前缀的链接/重定向"""
    if not path.startswith("/"):
        path = "/" + path
    return (YAOB_BASE + path) if YAOB_BASE else path

# =========================================================
# 数据持久化（JSON）
# =========================================================
class Store:
    def __init__(self):
        self.path = DATA_DIR
        self.cfg = self._load("cfg.json", {
            "open_margin": 5.0,
            "leverage": 5,
            "auto_trade_enabled": False,
            "margin_mode": "isolated",
            "exclude_large_cap": True,
            "strategy_states": {"a": True, "b": False, "c": False,
                                "d": False, "e": True, "f": True},
        })
        self.params = self._load("params.json", self._default_params())
        self.excluded = self._load("excluded.json", {
            "crypto": [], "index": []})
        self.stats = self._load("stats.json", self._default_stats())
        self.users = self._load("users.json", {})
        # 运行时
        self.rt = {
            "scanner_status": "⏳ 倒计时",
            "last_scan_duration": 0.0,
            "next_scan_timestamp": time.time() + 180,
            "scan_start_timestamp": 0,
            "account_total_assets": 0.0,
            "available_margin": 0.0,
            "candidate_pool": [],
            "positions": [],
        }
        self.api = {"key": "", "secret": ""}
        # 每用户运行时状态(候选池/持仓/资产等, 不落盘): {user: {...}}
        self.rt_by_user = {}
        # 自动开仓记录: {SYMBOL: {strategy, tp_ratio, sl_ratio, entry_price, open_time, qty}}
        # 用于自动平仓引擎按策略 tp/sl 对照实时浮盈/浮亏（手动单不在此列，不受自动平仓影响）
        self.open_records = self._load("open_records.json", {})

    def save_open_records(self): self._save("open_records.json", self.open_records)

    # ---------------- 用户隔离辅助 (8/3 改造) ----------------
    def _user_trade(self, user):
        """返回该用户的交易配置(trade子对象), 不存在则初始化默认."""
        if user not in self.users:
            self.users[user] = {}
        rec = self.users[user]
        if not isinstance(rec.get("trade"), dict):
            rec["trade"] = {
                "open_margin": 5.0, "leverage": 5,
                "auto_trade_enabled": False, "margin_mode": "isolated",
                "exclude_large_cap": True,
                "strategy_states": {"a": True, "b": False, "c": False,
                                    "d": False, "e": True, "f": True},
                "api": {"key": "", "secret": ""},
                "open_records": {},
                "trade_history": [],
            }
            self.save_users()
        return rec["trade"]

    def trade_cfg(self, user): return self._user_trade(user)
    def trade_api(self, user): return self._user_trade(user)["api"]
    def trade_open_records(self, user): return self._user_trade(user)["open_records"]

    def trade_history(self, user):
        """该用户历史成交列表(每次开仓留单, 平仓补充盈亏)."""
        return self._user_trade(user).get("trade_history", [])

    def trade_rt(self, user):
        """该用户运行时状态(候选池/持仓/资产等), 内存缓存."""
        if user not in self.rt_by_user:
            self.rt_by_user[user] = {
                "scanner_status": "⏳ 倒计时", "last_scan_duration": 0.0,
                "next_scan_timestamp": time.time() + 180, "scan_start_timestamp": 0,
                "account_total_assets": 0.0, "available_margin": 0.0,
                "candidate_pool": [], "positions": [],
            }
        return self.rt_by_user[user]

    def trade_params(self, user):
        """该用户策略参数(默认 A-F 原系统真实值, 可 per-user 覆盖)."""
        rec = self._user_trade(user)
        p = rec.get("params")
        if not isinstance(p, dict) or not p:
            # 用新鲜默认(原系统真实值), 不引用可能被旧 json 污染的 self.params
            p = self._default_params()
            rec["params"] = p
            self.save_users()
        return p


    def _default_params(self):
        # 以原系统 yang9527 真实 get_strategy_params 为准 (8/3 探测确认)
        return {
            "a": {"lookback_days": 66, "gain_min": 0.36, "gain_max": 0.50,
                  "vol_min": 1e7, "tp_ratio": 800, "sl_ratio": -20},
            "b": {"gain_threshold": 0.38, "vol_min": 1e7, "tp_ratio": 60, "sl_ratio": -20},
            "c": {"lookback_days": 7, "drop_threshold": 0.96, "vol_min": 1e8,
                  "tp_ratio": 100, "sl_ratio": -20},
            "d": {"window_minutes": 5, "gain_threshold": 0.05, "vol_min": 1e7,
                  "tp_ratio": 60, "sl_ratio": -20},
            "e": {"peak_gain_threshold": 0.50, "retrace_target_gain": 0.10,
                  "vol_min": 1e7, "tp_ratio": 1200, "sl_ratio": -86},
            "f": {"lookback_hours": 48, "fib_long": 0.786, "fib_short": 0.618,
                  "tolerance_ratio": 0.1, "vol_min": 3e7, "tp_ratio": 10, "sl_ratio": -15},
        }

    def _default_stats(self):
        return {"real": {
            "total_trades": 0, "win_trades": 0, "win_rate": 0,
            "total_pnl": 0.0, "daily_pnl": [], "days": [], "trade_counts": [],
        }}

    def _load(self, name, default):
        p = self.path / name
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return default

    def _save(self, name, data):
        (self.path / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def save_cfg(self): self._save("cfg.json", self.cfg)
    def save_params(self): self._save("params.json", self.params)
    def save_excluded(self): self._save("excluded.json", self.excluded)
    def save_stats(self): self._save("stats.json", self.stats)
    def save_users(self): self._save("users.json", self.users)


store = Store()

# =========================================================
# 用户 & 登录（Flask session）
# =========================================================
def current_user():
    return session.get("username")

def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*a, **kw):
        if not current_user():
            return redirect(_url("/login") + "?next=" + request.path)
        return f(*a, **kw)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        u = store.users.get(username)
        # 首个用户自动创建为 VIP（便于自测）；也可用种子账户
        if not u and username and store.users == {}:
            store.users[username] = {
                "password": generate_password_hash(password),
                "is_vip": True,
                "vip_expiry": "", "created": time.time()
            }
            store.save_users()
        u = store.users.get(username)
        if u and check_password_hash(u["password"], password):
            session["username"] = username
            next_url = request.args.get("next") or _url("/")
            return redirect(next_url)
        return render_template("login.html", error="用户名或密码错误")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if username in store.users:
            return render_template("register.html", error="用户名已存在")
        store.users[username] = {
            "password": generate_password_hash(password),
            "is_vip": False, "vip_expiry": "", "created": time.time()
        }
        store.save_users()
        session["username"] = username
        return redirect(_url("/"))
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(_url("/login"))


# =========================================================
# VIP 帮助
# =========================================================
def _is_vip():
    u = store.users.get(current_user() or "")
    if not u or not u.get("is_vip"):
        return False
    exp = u.get("vip_expiry", "")
    if exp:
        try:
            if time.time() > time.mktime(time.strptime(exp, "%Y-%m-%d %H:%M:%S")):
                return False
        except Exception:
            pass
    return True


# 管理员账号（环境变量可覆盖）。对应用户可进入管理后台给其它账号授权 VIP。
ADMIN_USER = os.getenv("ADMIN_USER", "XJarvis")


def _is_admin():
    return current_user() == ADMIN_USER


@app.context_processor
def _inject_admin():
    return {"admin_user": ADMIN_USER, "is_admin": _is_admin()}



# =========================================================
# 页面
# =========================================================
@app.route("/")
@login_required
def index():
    return render_template("index.html", username=current_user(),
                           is_vip=_is_vip(),
                           vip_expiry=(store.users.get(current_user()) or {}).get("vip_expiry",""))


# =========================================================
# 管理后台（仅管理员）—— 授权/撤销 VIP、删除账号
# =========================================================
def require_admin(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*a, **kw):
        if not _is_admin():
            if request.path.startswith("/api/"):
                return jsonify({"status": "error", "msg": "无管理权限"}), 403
            return redirect(_url("/login"))
        return f(*a, **kw)
    return wrapper


@app.route("/admin")
@require_admin
def admin_page():
    return render_template("admin.html", username=current_user())


@app.route("/api/admin/users")
@require_admin
def admin_users():
    rows = []
    for name, u in sorted(store.users.items(), key=lambda kv: kv[1].get("created", 0)):
        rows.append({
            "username": name,
            "is_vip": bool(u.get("is_vip")),
            "vip_expiry": u.get("vip_expiry", ""),
            "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(u.get("created", 0))),
            "is_admin": (name == ADMIN_USER),
        })
    return jsonify({"status": "success", "admin": ADMIN_USER, "users": rows})


@app.route("/api/admin/set_vip", methods=["POST"])
@require_admin
def admin_set_vip():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    on = bool(data.get("vip"))
    # days: 有效天数。0 或未提供 = 永久；否则按天数算到期时间
    try:
        days = int(data.get("days", 0) or 0)
    except (TypeError, ValueError):
        days = 0
    if username == ADMIN_USER:
        return jsonify({"status": "error", "msg": "不能修改管理员自身 VIP"})
    u = store.users.get(username)
    if not u:
        return jsonify({"status": "error", "msg": f"账号 {username} 不存在"})
    u["is_vip"] = on
    if on:
        u["vip_expiry"] = "" if days <= 0 else time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() + days * 86400))
        msg = f"已授予 {username} VIP" + ("（永久）" if days <= 0 else f"，{days} 天后到期")
    else:
        u["vip_expiry"] = ""
        msg = f"已撤销 {username} 的 VIP"
    store.save_users()
    return jsonify({"status": "success", "msg": msg,
                    "username": username, "vip": on, "vip_expiry": u["vip_expiry"]})


@app.route("/api/admin/delete_user", methods=["POST"])
@require_admin
def admin_delete_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if username == ADMIN_USER:
        return jsonify({"status": "error", "msg": "不能删除管理员账号"})
    if username not in store.users:
        return jsonify({"status": "error", "msg": f"账号 {username} 不存在"})
    del store.users[username]
    store.save_users()
    return jsonify({"status": "success", "msg": f"已删除账号 {username}", "username": username})



# =========================================================
# API: 仪表盘
# =========================================================
@app.route("/api/dashboard")
@login_required
def api_dashboard():
    user = current_user()
    rt = store.trade_rt(user)
    tc = store.trade_cfg(user)
    u = store.users.get(user) or {}
    return jsonify({
        "scanner_status": rt["scanner_status"],
        "last_scan_duration": rt["last_scan_duration"],
        "next_scan_timestamp": rt["next_scan_timestamp"],
        "scan_start_timestamp": rt["scan_start_timestamp"],
        "account_total_assets": rt["account_total_assets"],
        "available_margin": rt["available_margin"],
        "open_margin": tc["open_margin"],
        "leverage": tc["leverage"],
        "auto_trade_enabled": tc["auto_trade_enabled"],
        "margin_mode": tc["margin_mode"],
        "exclude_large_cap": tc["exclude_large_cap"],
        "has_api_key": bool(tc.get("api", {}).get("key")),
        "is_vip": _is_vip(),
        "vip_expiry": u.get("vip_expiry", ""),
        "strategy_states": tc["strategy_states"],
        "candidate_pool": rt["candidate_pool"],
        "positions": rt["positions"],
    })


@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify({"real": store.stats["real"]})


@app.route("/api/control", methods=["POST"])
@login_required
def api_control():
    d = request.get_json(silent=True) or {}
    tc = store.trade_cfg(current_user())
    if "open_margin" in d:
        try: tc["open_margin"] = float(d["open_margin"])
        except Exception: pass
    if "leverage" in d:
        try: tc["leverage"] = int(d["leverage"])
        except Exception: pass
    store.save_users()
    return jsonify({"status": "success"})


# =========================================================
# API: 币安密钥
# =========================================================
@app.route("/api/set_api_keys", methods=["POST"])
@login_required
def set_api_keys():
    d = request.get_json(silent=True) or {}
    k = (d.get("api_key") or "").strip()
    s = (d.get("api_secret") or "").strip()
    if not k or not s:
        return jsonify({"status": "error", "msg": "请完整填写API Key和Secret"})
    ta = store.trade_api(current_user())
    ta["key"] = k; ta["secret"] = s
    store.save_users()
    return jsonify({"status": "success", "msg": "保存成功"})


@app.route("/api/clear_api_keys", methods=["POST"])
@login_required
def clear_api_keys():
    ta = store.trade_api(current_user())
    ta["key"] = ""; ta["secret"] = ""
    store.save_users()
    return jsonify({"status": "success", "msg": "已清除"})


# =========================================================
# API: 开关
# =========================================================
@app.route("/api/toggle_auto_trade", methods=["POST"])
@login_required
def toggle_auto_trade():
    tc = store.trade_cfg(current_user())
    tc["auto_trade_enabled"] = not tc["auto_trade_enabled"]
    store.save_users()
    return jsonify({"status": "success",
                    "auto_trade_enabled": tc["auto_trade_enabled"]})


@app.route("/api/toggle_margin_mode", methods=["POST"])
@login_required
def toggle_margin_mode():
    tc = store.trade_cfg(current_user())
    tc["margin_mode"] = "cross" if tc["margin_mode"] == "isolated" else "isolated"
    store.save_users()
    return jsonify({"status": "success", "margin_mode": tc["margin_mode"]})


@app.route("/api/toggle_exclude_large_cap", methods=["POST"])
@login_required
def toggle_exclude_large_cap():
    tc = store.trade_cfg(current_user())
    tc["exclude_large_cap"] = not tc["exclude_large_cap"]
    store.save_users()
    return jsonify({"status": "success",
                    "exclude_large_cap": tc["exclude_large_cap"]})


@app.route("/api/toggle_strategy", methods=["POST"])
@login_required
def toggle_strategy():
    # 线上版：所有策略切换均需 VIP
    if not _is_vip():
        return jsonify({"status": "error", "msg": "请先升级为VIP会员"})
    d = request.get_json(silent=True) or {}
    s = (d.get("strategy") or "a").lower()
    tc = store.trade_cfg(current_user())
    if s not in tc["strategy_states"]:
        return jsonify({"status": "error", "msg": "未知策略"})
    tc["strategy_states"][s] = not tc["strategy_states"][s]
    store.save_users()
    return jsonify({"status": "success", "strategy": s,
                    "enabled": tc["strategy_states"][s]})


# =========================================================
# API: 策略参数
# =========================================================
@app.route("/api/get_strategy_params")
@login_required
def get_strategy_params():
    return jsonify(store.trade_params(current_user()))


@app.route("/api/save_strategy_params", methods=["POST"])
@login_required
def save_strategy_params():
    d = request.get_json(silent=True) or {}
    sp = d.get("strategy_params")
    if not isinstance(sp, dict):
        return jsonify({"status": "error", "msg": "参数格式错误"})
    params = store.trade_params(current_user())
    for k in params:
        if isinstance(sp.get(k), dict):
            for kk, v in sp[k].items():
                if kk in params[k]:
                    try: params[k][kk] = float(v)
                    except Exception: pass
    store.save_users()
    return jsonify({"status": "success"})


# =========================================================
# API: 黑名单
# =========================================================
@app.route("/api/get_excluded_symbols_categorized")
@login_required
def get_excluded():
    return jsonify({"status": "success",
                    "crypto": store.excluded["crypto"],
                    "index": store.excluded["index"]})


@app.route("/api/add_excluded_symbols", methods=["POST"])
@login_required
def add_excluded():
    d = request.get_json(silent=True) or {}
    syms = [str(s).upper() for s in (d.get("symbols") or []) if s]
    added = []
    for s in syms:
        # 带 /USDT 结尾判为币种，否则股指
        if "/USDT" in s or (s.endswith("USDT") and not s in store.excluded["index"]):
            target = "crypto"
        else:
            target = "index"
        if s not in store.excluded[target]:
            store.excluded[target].append(s); added.append(s)
    store.save_excluded()
    return jsonify({"status": "success", "added": added})


@app.route("/api/remove_excluded_symbols", methods=["POST"])
@login_required
def remove_excluded():
    d = request.get_json(silent=True) or {}
    syms = [str(s).upper() for s in (d.get("symbols") or [])]
    store.excluded["crypto"] = [s for s in store.excluded["crypto"] if s not in syms]
    store.excluded["index"] = [s for s in store.excluded["index"] if s not in syms]
    store.save_excluded()
    return jsonify({"status": "success"})


@app.route("/api/clear_excluded_symbols", methods=["POST"])
@login_required
def clear_excluded():
    store.excluded["crypto"] = []
    store.excluded["index"] = []
    store.save_excluded()
    return jsonify({"status": "success"})


@app.route("/api/restore_default_excluded", methods=["POST"])
@login_required
def restore_default_excluded():
    DEFAULT_CRYPTO = [
        "BTC/USDT","ETH/USDT","BNB/USDT","SOL/USDT","XRP/USDT","ADA/USDT",
        "DOGE/USDT","DOT/USDT","LINK/USDT","LTC/USDT","BCH/USDT","AVAX/USDT",
        "SHIB/USDT","TON/USDT","TRX/USDT","UNI/USDT","ATOM/USDT","XLM/USDT",
        "FIL/USDT","SUI/USDT","NEAR/USDT","APT/USDT","ARB/USDT","OP/USDT",
        "INJ/USDT","SEI/USDT","HBAR/USDT","ICP/USDT","RENDER/USDT","WIF/USDT",
        "TRUMP/USDT","1000PEPE/USDT","ETC/USDT",
    ]
    store.excluded["crypto"] = DEFAULT_CRYPTO
    store.excluded["index"] = []
    store.save_excluded()
    return jsonify({"status": "success", "count": len(DEFAULT_CRYPTO)})


# =========================================================
# API: 告警 / 统计
# =========================================================
@app.route("/api/test_alert", methods=["POST"])
@login_required
def test_alert():
    print(f"[alert] 告警测试 from {current_user()} at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    return jsonify({"status": "success"})


@app.route("/api/reset_stats", methods=["POST"])
@login_required
def reset_stats():
    store.stats["real"] = {"total_trades": 0, "win_trades": 0, "win_rate": 0,
                           "total_pnl": 0.0, "daily_pnl": [], "days": [],
                           "trade_counts": []}
    store.save_stats()
    return jsonify({"status": "success"})


@app.route("/api/trade_history", methods=["GET"])
@login_required
def api_trade_history():
    """历史成交记录列表(倒序: 最新在前)."""
    hist = store.trade_history(current_user())
    return jsonify({"trades": list(reversed(hist)), "total": len(hist)})


@app.route("/api/strategy_stats", methods=["GET"])
@login_required
def api_strategy_stats():
    """按策略的历史表现统计: 次数/胜率/累计盈亏/止盈止损."""
    hist = store.trade_history(current_user())
    closed = [h for h in hist if h.get("status") == "CLOSED"]
    strat = {}
    for h in closed:
        s = h.get("strategy", "A")
        st = strat.setdefault(s, {"trades": 0, "wins": 0, "pnl_sum": 0.0,
                                  "tp": 0, "sl": 0, "manual": 0, "open_pnl": 0})
        st["trades"] += 1
        r = h.get("pnl_ratio")
        if r is not None:
            st["pnl_sum"] += r
            if r > 0: st["wins"] += 1
            if h.get("close_reason") == "tp": st["tp"] += 1
            elif h.get("close_reason") == "sl": st["sl"] += 1
            else: st["manual"] += 1
        else:
            st["open_pnl"] += 1
    out = []
    for s, st in strat.items():
        out.append({
            "strategy": s,
            "type": STAT.get(s, s),
            "trades": st["trades"],
            "win_rate": round(st["wins"] / st["trades"] * 100, 1) if st["trades"] else 0,
            "pnl_sum": round(st["pnl_sum"], 2),
            "tp": st["tp"], "sl": st["sl"], "manual": st["manual"],
        })
    out.sort(key=lambda x: -x["trades"])
    return jsonify({"strategy_stats": out})


@app.route("/api/trade_profit_stats", methods=["GET"])
@login_required
def api_trade_profit_stats():
    """按天分布: 每天盈亏/交易次数/胜率."""
    hist = store.trade_history(current_user())
    closed = [h for h in hist if h.get("status") == "CLOSED"]
    daily = {}
    for h in closed:
        day = (h.get("close_time") or h.get("open_time") or "")[:10]
        if not day: continue
        d = daily.setdefault(day, {"pnl": 0.0, "trades": 0, "wins": 0})
        d["trades"] += 1
        r = h.get("pnl_ratio")
        if r is not None:
            d["pnl"] += r
            if r > 0: d["wins"] += 1
    out = []
    for d, v in sorted(daily.items(), reverse=True):
        out.append({"day": d, "pnl": round(v["pnl"], 2), "trades": v["trades"],
                    "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0})
    # 按标的分布
    bysym = {}
    for h in closed:
        s = h.get("symbol", ""); b = bysym.setdefault(s, {"trades": 0, "pnl": 0.0, "wins": 0})
        b["trades"] += 1; r = h.get("pnl_ratio")
        if r is not None:
            b["pnl"] += r
            if r > 0: b["wins"] += 1
    syms = [{"symbol": s, "trades": v["trades"], "pnl": round(v["pnl"], 2),
             "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0}
            for s, v in sorted(bysym.items(), key=lambda x: -x[1]["pnl"])]
    return jsonify({"daily": out, "by_symbol": syms})


# =========================================================
# 扫描引擎（币安 FAPI 真实实现，走本机代理）
# =========================================================
from fapi import FapiClient, BinanceError

# 全市场扫描（USDT 永续合约）。可用环境变量限定范围便于调试
SCAN_SYMBOLS = os.getenv("SCAN_SYMBOLS", "")

fapi = FapiClient()

STAT = {"A": "空", "B": "空", "C": "多", "D": "空", "E": "多", "F": "斐波那契双向"}


def _pct(a, b):
    if not b:
        return 0.0
    return (a - b) / b * 100.0


def _norm_symbol(sym):
    """FAPI 'ZECUSDT' -> 显示 'ZEC/USDT'"""
    if sym.endswith("USDT"):
        return sym[:-4] + "/USDT"
    return sym


def _excluded_set():
    return set(store.excluded["crypto"] + store.excluded["index"])


# ---------- 策略真实检测（基于 24h ticker 单次拉取，秒级全市场扫描） ----------
# tick 字段: lastPrice, openPrice, highPrice, lowPrice, priceChangePercent, quoteVolume

def _kt(t, k):
    return float(t.get(k, 0))


def check_a(tick, p):
    """做空(A): 24h涨幅在[下限%,上限%] + 成交额达标"""
    g24 = _kt(tick, "priceChangePercent") / 100.0
    qv = _kt(tick, "quoteVolume")
    if p["gain_min"] <= g24 <= p["gain_max"] and qv >= p["vol_min"]:
        return {"strategy": "A", "direction": "SHORT",
                "reason": f"24h涨幅{g24*100:.1f}%", "threshold_ratio": g24 * 100}
    return None


def check_b(tick, p):
    """做空(B): 当日涨幅>=N%(用开→现近似) + 成交额达标"""
    o = _kt(tick, "openPrice"); c = _kt(tick, "lastPrice")
    g = _pct(c, o) / 100.0
    if g >= p["gain_threshold"] and _kt(tick, "quoteVolume") >= p["vol_min"]:
        return {"strategy": "B", "direction": "SHORT",
                "reason": f"当日涨幅{g*100:.1f}%", "threshold_ratio": g * 100}
    return None


def check_c(tick, p):
    """做多(C): 从当日高点回撤>=M% + 成交额达标"""
    hi = _kt(tick, "highPrice"); c = _kt(tick, "lastPrice")
    drop = _pct(c, hi) / 100.0  # 负值=回撤
    if hi > 0 and drop <= -abs(p["drop_threshold"]) and _kt(tick, "quoteVolume") >= p["vol_min"]:
        return {"strategy": "C", "direction": "LONG",
                "reason": f"自高点回撤{abs(drop)*100:.1f}%", "threshold_ratio": drop * 100}
    return None


def check_d(fapi, sym, tick, p):
    """做空(D): N分钟涨幅>=N% (需1m k线)"""
    w = int(p["window_minutes"])
    try:
        k = fapi.klines(sym, "1m", w + 1)
    except Exception:
        return None
    if len(k) < w + 1:
        return None
    p0 = float(k[-w - 1][4]); p1 = float(k[-1][4])
    g = _pct(p1, p0)
    if g >= p["gain_threshold"] * 100 and _kt(tick, "quoteVolume") >= p["vol_min"]:
        return {"strategy": "D", "direction": "SHORT",
                "reason": f"{w}分钟涨幅{g:.1f}%", "threshold_ratio": g}
    return None


def check_e(tick, p):
    """做多(E): 冲高>=N%后回落至<=M% + 成交额达标"""
    o = _kt(tick, "openPrice"); hi = _kt(tick, "highPrice"); c = _kt(tick, "lastPrice")
    hi_g = _pct(hi, o); cur_g = _pct(c, o)
    if hi_g >= p["peak_gain_threshold"] * 100 and cur_g <= p["retrace_target_gain"] * 100 \
            and cur_g > 0 and _kt(tick, "quoteVolume") >= p["vol_min"]:
        return {"strategy": "E", "direction": "LONG",
                "reason": f"冲高{hi_g:.1f}%回落至{cur_g:.1f}%", "threshold_ratio": cur_g}
    return None


def check_f(tick, p):
    """做多/做空(F): 斐波那契 用24h高低点近似 + 成交额达标（对齐线上文案）"""
    hi = _kt(tick, "highPrice"); lo = _kt(tick, "lowPrice"); cur = _kt(tick, "lastPrice")
    rng = hi - lo
    if rng <= 0 or _kt(tick, "quoteVolume") < p["vol_min"]:
        return None
    fl = float(p["fib_long"]); fs = float(p["fib_short"])
    tol = float(p["tolerance_ratio"]) * rng
    if abs(cur - (lo + rng * fs)) <= tol:
        return {"strategy": "F", "direction": "SHORT",
                "reason": f"斐波那契做空(反弹至{fs*100:.1f}%, 阻力{lo+rng*fs:.4f})",
                "threshold_ratio": fs * 100}
    if abs(cur - (hi - rng * fl)) <= tol:
        return {"strategy": "F", "direction": "LONG",
                "reason": f"斐波那契做多(回撤至{fl*100:.1f}%, 支撑{hi-rng*fl:.4f})",
                "threshold_ratio": fl * 100}
    return None


CHECKS = {"a": lambda f, s, t, p: check_a(t, p),
          "b": lambda f, s, t, p: check_b(t, p),
          "c": lambda f, s, t, p: check_c(t, p),
          "d": check_d,
          "e": lambda f, s, t, p: check_e(t, p),
          "f": lambda f, s, t, p: check_f(t, p)}


def _candidates(tickers, user):
    """扫描所有开着策略的币种，生成候选池(按用户隔离: 用该用户的strategy_states/params/exclude)
    优化: 先用 ticker 24h数据粗筛(涨幅/成交额), 命中才调 klines 细查
    """
    tc = store.trade_cfg(user)
    states = tc["strategy_states"]
    params = store.trade_params(user)
    excluded = _excluded_set()
    pool = []
    if SCAN_SYMBOLS:
        symbols = [s.upper().replace("/", "") for s in SCAN_SYMBOLS.split(",")]
    else:
        symbols = [s for s in tickers if s.endswith("USDT")]
    # 粗筛: 记录每个币最低所需成交额(开着策略中最低的vol_min)
    min_vol = min([params[k]["vol_min"] for k in "abcdef" if states.get(k)] or [0])
    for sym in symbols[:600]:
        try:
            tick = tickers.get(sym)
            if not tick:
                continue
            if tc["exclude_large_cap"] and sym in excluded:
                continue
            qv = float(tick.get("quoteVolume", 0))
            if qv == 0 or qv < min_vol:
                continue
            # 粗筛涨幅: 24h幅度过低时多数策略不触发(仅跳过极端情况)
            for sk, fn in CHECKS.items():
                if not states.get(sk):
                    continue
                sig = fn(fapi, sym, tick, params[sk])
                if sig:
                    sig["symbol"] = _norm_symbol(sym)
                    sig["current_price"] = float(tick["lastPrice"])
                    sig["priority"] = float(tick.get("priceChangePercent", 0)) / 100.0
                    sig["trigger_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    sig["unopen_reason"] = "等待开仓"
                    pool.append(sig)
                    break  # 每币优先取一个信号
        except (BinanceError, Exception):
            continue
    # 原系统排序: 先按方向分组(SHORT在前/LONG在后), 组内按 priority 降序 (8/3 探测确认)
    shorts = [c for c in pool if c.get("direction") == "SHORT"]
    longs = [c for c in pool if c.get("direction") != "SHORT"]
    shorts.sort(key=lambda x: x["priority"], reverse=True)
    longs.sort(key=lambda x: x["priority"], reverse=True)
    return (shorts + longs)[:10]


def run_scan(user=None):
    """按用户执行一次扫描: 生成候选池 + 更新持仓/资产 + 自动平仓/开仓.
    用户隔离: 每用户用自己的 api/策略状态/参数/开仓记录."""
    global fapi
    if not user:
        try:
            user = current_user()  # 单用户/请求触发时兜底
        except Exception:
            user = next(iter(store.users), None)  # 无request context时用任意用户
    if not user:
        return  # 无可用用户则不扫描
    rt = store.trade_rt(user)
    tc = store.trade_cfg(user)
    ta = store.trade_api(user)
    tor = store.trade_open_records(user)
    params = store.trade_params(user)
    rt["scanner_status"] = "正在扫描..."
    rt["scan_start_timestamp"] = int(time.time())
    st = time.time()
    try:
        fapi.set_api_keys(ta.get("key", ""), ta.get("secret", ""))
        tickers = {t["symbol"]: t for t in fapi.all_tickers()}
        pool = _candidates(tickers, user)
        rt["candidate_pool"] = pool
        # 真实账户资产 + 持仓
        if not fapi._dry_run:
            acct = fapi.account()
            rt["account_total_assets"] = float(acct.get("totalMarginBalance", 0))
            rt["available_margin"] = float(acct.get("availableBalance", 0))
            pos = []
            for p in acct.get("positions", []):
                amt = float(p.get("positionAmt", 0))
                if amt == 0:
                    continue
                sym = _norm_symbol(p["symbol"])
                mg = float(p.get("initialMargin", 0))
                pnl = float(p.get("unrealizedProfit", 0))
                pos.append({
                    "symbol": sym, "direction": "LONG" if amt > 0 else "SHORT",
                    "amount": abs(amt), "margin": mg,
                    "leverage": p.get("leverage"), "open_time": "",
                    "open_reason": "", "current_price": float(p.get("entryPrice", 0)),
                    "pnl": pnl, "pnl_ratio": pnl / mg * 100 if mg else 0,
                })
            rt["positions"] = pos
        # 自动交易：先自动平仓检查，再对高优先级候选开仓
        if tc["auto_trade_enabled"] and ta.get("key") and not fapi._dry_run:
            # ---- 自动平仓引擎：按策略 tp_ratio / sl_ratio 对照实时浮盈亏损 ----
            # 仅处理系统自动开仓记录(open_records)，手动单不受影响
            try:
                acct0 = fapi.account()
                live = {p["symbol"]: p for p in acct0.get("positions", []) if float(p.get("positionAmt", 0)) != 0}
            except Exception:
                live = {}
            if tor:
                for sym0 in list(tor.keys()):
                    rec = tor[sym0]
                    pos = live.get(sym0)
                    if not pos:
                        # 该币已无持仓（手动平/已平/强平），清理残留记录并回填为 manual
                        reason = "manual"
                        hist = store.trade_history(user)
                        for h in reversed(hist):
                            if h.get("symbol") == sym0 and h.get("status") == "OPEN":
                                h["status"] = "CLOSED"
                                h["close_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                                h["close_reason"] = reason
                                h["pnl_ratio"] = None
                                break
                        tor.pop(sym0, None); store.save_users()
                        continue
                    entry = float(rec.get("entry_price", 0))
                    last = float(pos.get("lastPrice") or pos.get("entryPrice", 0))  # 实际成交均价兜底
                    # 用实时最新价计算浮盈/浮亏比例
                    try:
                        tick = tickers.get(sym0)
                        if not tick:
                            tick = tickers.get(sym0.rstrip("USDT") + "USDT")
                        if tick:
                            last = float(tick["lastPrice"])
                    except Exception:
                        pass
                    amt = float(pos.get("positionAmt", 0))
                    if entry <= 0:
                        mg = float(pos.get("initialMargin", 0))
                        pnl = float(pos.get("unrealizedProfit", 0))
                        ratio = pnl / mg * 100 if mg else 0
                    elif amt > 0:  # 多头
                        ratio = (last - entry) / entry * 100
                    else:  # 空头
                        ratio = (entry - last) / entry * 100
                    tp = float(rec.get("tp_ratio", 0))
                    sl = float(rec.get("sl_ratio", 0))
                    qty = abs(amt)
                    if qty <= 0:
                        continue
                    side = "SELL" if amt > 0 else "BUY"
                    # 触发止盈(stop_profit) 或 止损(stop_loss)
                    closed = False
                    if tp > 0 and ratio >= tp:
                        print(f"[auto-close:{user}] {sym0} 止盈 ratio={ratio:.2f}% >= tp={tp}")
                        closed = True
                    elif sl < 0 and ratio <= sl:
                        print(f"[auto-close:{user}] {sym0} 止损 ratio={ratio:.2f}% <= sl={sl}")
                        closed = True
                    if closed:
                        try:
                            fapi.close_position(sym0, qty, side=side)
                            print(f"[auto-close:{user}] {sym0} 已平 {qty} ({side})")
                            # 回填历史成交记录(OPEN->CLOSED, 记平仓价/盈亏/结果)
                            reason = "tp" if (tp > 0 and ratio >= tp) else ("sl" if (sl < 0 and ratio <= sl) else "manual")
                            same_price_match = "open_price==rec.entry"
                            hist = store.trade_history(user)
                            for h in reversed(hist):  # 倒序找该币最近一条OPEN
                                if h.get("symbol") == sym0 and h.get("status") == "OPEN" and h.get("open_price") == entry:
                                    h["status"] = "CLOSED"
                                    h["close_price"] = last
                                    h["close_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                                    h["close_reason"] = reason
                                    h["pnl_ratio"] = round(ratio, 4)
                                    break
                            tor.pop(sym0, None)
                            store.save_users()
                        except BinanceError as e:
                            print(f"[auto-close:{user}] {sym0} 平仓失败: {e}")

            # 当前真实持仓币种集合（大小写归一）
            held = set()
            try:
                acct = fapi.account()
                for p in acct.get("positions", []):
                    if float(p.get("positionAmt", 0)) != 0:
                        held.add(p["symbol"])
            except Exception:
                held = set()  # 拿不到持仓时不阻断扫描
            opened = []
            # 原系统开仓保证金硬门槛: 可用保证金 >= open_margin 才开, 否则拒开(8/3探测确认)
            avail = rt.get("available_margin") or 0
            open_margin = tc["open_margin"]
            for cand in pool[:3]:
                sym0 = cand["symbol"].replace("/", "").upper()
                if sym0 in held:
                    continue  # 已持仓，不重复开仓
                # 保证金门槛(原系统 unopen_reason=保证金不足(x<y))
                if avail < open_margin:
                    cand["unopen_reason"] = f"保证金不足({avail:.2f}<{open_margin:.2f})"
                    continue
                side = "SELL" if cand["direction"] == "SHORT" else "BUY"
                price = cand["current_price"]
                qty = open_margin * tc["leverage"] / price if price > 0 else 0
                if qty <= 0:
                    continue
                try:
                    fapi.set_leverage(sym0, tc["leverage"])
                    fapi.new_order(sym0, side, qty)
                    opened.append(cand["symbol"])
                    avail -= open_margin  # 每开一单扣减保证金配额
                    # 记录该笔自动单，用于后续自动平仓(按策略 tp/sl)
                    now_s = time.strftime("%Y-%m-%d %H:%M:%S")
                    tor[sym0] = {
                        "strategy": cand.get("strategy", ""),
                        "tp_ratio": float(params[cand.get("strategy", "")]["tp_ratio"]) if cand.get("strategy") in params else 0,
                        "sl_ratio": float(params[cand.get("strategy", "")]["sl_ratio"]) if cand.get("strategy") in params else 0,
                        "entry_price": price, "open_time": now_s,
                        "qty": qty,
                    }
                    # 历史成交落盘: 开仓记一笔(OPEN), 平仓时用 symbol 回填结果
                    store.trade_history(user).append({
                        "symbol": sym0, "direction": cand.get("direction", ""),
                        "strategy": cand.get("strategy", ""), "qty": qty,
                        "open_price": price, "open_time": now_s,
                        "close_price": None, "close_time": None,
                        "pnl_ratio": None, "close_reason": None,  # tp/sl/manual
                        "status": "OPEN",
                    })
                    store.save_users()
                    held.add(sym0)  # 本轮内同币不再重复尝试
                except BinanceError as e:
                    print(f"[auto-trade:{user}] {sym0} 开仓失败: {e}")
            if opened:
                print(f"[auto-trade:{user}] 开仓 {opened}")
        rt["scanner_status"] = "⏳ 倒计时"
        rt["last_scan_duration"] = round(time.time() - st, 1)
        rt["next_scan_timestamp"] = time.time() + 60
    except BinanceError as e:
        print(f"[scan:{user}] FAPI错误: {e}")
        rt["scanner_status"] = f"行情获取失败"
    except Exception as e:
        print(f"[scan:{user}] err: {e}")
        rt["scanner_status"] = "扫描异常"


def _active_traders():
    """需要自动交易的用户: 有 API key 且 auto_trade 开启(优先), 否则至少配了 key."""
    out = []
    for u, rec in store.users.items():
        t = rec.get("trade")
        if not t:
            continue
        if t.get("api", {}).get("key") and t.get("auto_trade_enabled"):
            out.append(u)
        elif t.get("api", {}).get("key") and not out:
            out.append(u)  # 有key但没开auto_trade的用户至少更新行情/持仓
    return out


def scan_loop():
    while True:
        try:
            traders = _active_traders()
            if traders:
                for u in traders:
                    run_scan(u)
            else:
                run_scan(admin_default_user() if False else None)
        except Exception as e:
            print("[scan] loop err", e)
        time.sleep(60)


def admin_default_user():
    """后台兜底: 返回唯一/管理员用户, 供无活跃交易者时也更新面板."""
    for u in store.users:
        return u
    return None


def start_scanner():
    t = threading.Thread(target=scan_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    start_scanner()
    port = int(os.getenv("PORT", "8100"))
    print(f"妖币系统后端运行于 http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
