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

    def _default_params(self):
        return {
            "a": {"lookback_days": 3, "gain_min": 0.30, "gain_max": 0.60,
                  "vol_min": 1e7, "tp_ratio": 180, "sl_ratio": -20},
            "b": {"gain_threshold": 0.30, "vol_min": 1e7, "tp_ratio": 90, "sl_ratio": -20},
            "c": {"lookback_days": 3, "drop_threshold": 0.20, "vol_min": 1e7,
                  "tp_ratio": 210, "sl_ratio": -20},
            "d": {"window_minutes": 5, "gain_threshold": 0.05, "vol_min": 1e7,
                  "tp_ratio": 60, "sl_ratio": -20},
            "e": {"peak_gain_threshold": 0.50, "retrace_target_gain": 0.10,
                  "vol_min": 1e7, "tp_ratio": 1200, "sl_ratio": -20},
            "f": {"lookback_hours": 48, "fib_long": 0.618, "fib_short": 0.382,
                  "tolerance_ratio": 0.005, "vol_min": 3e7, "tp_ratio": 120, "sl_ratio": -20},
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
    rt = store.rt
    u = store.users.get(current_user()) or {}
    return jsonify({
        "scanner_status": rt["scanner_status"],
        "last_scan_duration": rt["last_scan_duration"],
        "next_scan_timestamp": rt["next_scan_timestamp"],
        "scan_start_timestamp": rt["scan_start_timestamp"],
        "account_total_assets": rt["account_total_assets"],
        "available_margin": rt["available_margin"],
        "open_margin": store.cfg["open_margin"],
        "leverage": store.cfg["leverage"],
        "auto_trade_enabled": store.cfg["auto_trade_enabled"],
        "margin_mode": store.cfg["margin_mode"],
        "exclude_large_cap": store.cfg["exclude_large_cap"],
        "has_api_key": bool(store.api.get("key")),
        "is_vip": _is_vip(),
        "vip_expiry": u.get("vip_expiry", ""),
        "strategy_states": store.cfg["strategy_states"],
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
    if "open_margin" in d:
        try: store.cfg["open_margin"] = float(d["open_margin"])
        except Exception: pass
    if "leverage" in d:
        try: store.cfg["leverage"] = int(d["leverage"])
        except Exception: pass
    store.save_cfg()
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
    store.api = {"key": k, "secret": s}
    return jsonify({"status": "success", "msg": "保存成功"})


@app.route("/api/clear_api_keys", methods=["POST"])
@login_required
def clear_api_keys():
    store.api = {"key": "", "secret": ""}
    return jsonify({"status": "success", "msg": "已清除"})


# =========================================================
# API: 开关
# =========================================================
@app.route("/api/toggle_auto_trade", methods=["POST"])
@login_required
def toggle_auto_trade():
    store.cfg["auto_trade_enabled"] = not store.cfg["auto_trade_enabled"]
    store.save_cfg()
    return jsonify({"status": "success",
                    "auto_trade_enabled": store.cfg["auto_trade_enabled"]})


@app.route("/api/toggle_margin_mode", methods=["POST"])
@login_required
def toggle_margin_mode():
    store.cfg["margin_mode"] = "cross" if store.cfg["margin_mode"] == "isolated" else "isolated"
    store.save_cfg()
    return jsonify({"status": "success", "margin_mode": store.cfg["margin_mode"]})


@app.route("/api/toggle_exclude_large_cap", methods=["POST"])
@login_required
def toggle_exclude_large_cap():
    store.cfg["exclude_large_cap"] = not store.cfg["exclude_large_cap"]
    store.save_cfg()
    return jsonify({"status": "success"})


@app.route("/api/toggle_strategy", methods=["POST"])
@login_required
def toggle_strategy():
    # 线上版：所有策略切换均需 VIP
    if not _is_vip():
        return jsonify({"status": "error", "msg": "请先升级为VIP会员"})
    d = request.get_json(silent=True) or {}
    s = (d.get("strategy") or "a").lower()
    if s not in store.cfg["strategy_states"]:
        return jsonify({"status": "error", "msg": "未知策略"})
    store.cfg["strategy_states"][s] = not store.cfg["strategy_states"][s]
    store.save_cfg()
    return jsonify({"status": "success", "strategy": s,
                    "enabled": store.cfg["strategy_states"][s]})


# =========================================================
# API: 策略参数
# =========================================================
@app.route("/api/get_strategy_params")
@login_required
def get_strategy_params():
    return jsonify(store.params)


@app.route("/api/save_strategy_params", methods=["POST"])
@login_required
def save_strategy_params():
    d = request.get_json(silent=True) or {}
    sp = d.get("strategy_params")
    if not isinstance(sp, dict):
        return jsonify({"status": "error", "msg": "参数格式错误"})
    for k in store.params:
        if isinstance(sp.get(k), dict):
            for kk, v in sp[k].items():
                if kk in store.params[k]:
                    try: store.params[k][kk] = float(v)
                    except Exception: pass
    store.save_params()
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


def _candidates(tickers):
    """扫描所有开着策略的币种，生成候选池
    优化: 先用 ticker 24h数据粗筛(涨幅/成交额), 命中才调 klines 细查
    """
    states = store.cfg["strategy_states"]
    excluded = _excluded_set()
    pool = []
    if SCAN_SYMBOLS:
        symbols = [s.upper().replace("/", "") for s in SCAN_SYMBOLS.split(",")]
    else:
        symbols = [s for s in tickers if s.endswith("USDT")]
    # 粗筛: 记录每个币最低所需成交额(开着策略中最低的vol_min)
    min_vol = min([store.params[k]["vol_min"] for k in "abcdef" if states.get(k)] or [0])
    for sym in symbols[:600]:
        try:
            tick = tickers.get(sym)
            if not tick:
                continue
            if store.cfg["exclude_large_cap"] and sym in excluded:
                continue
            qv = float(tick.get("quoteVolume", 0))
            if qv == 0 or qv < min_vol:
                continue
            # 粗筛涨幅: 24h幅度过低时多数策略不触发(仅跳过极端情况)
            for sk, fn in CHECKS.items():
                if not states.get(sk):
                    continue
                sig = fn(fapi, sym, tick, store.params[sk])
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
    pool.sort(key=lambda x: x["priority"], reverse=True)
    return pool[:10]


def run_scan():
    global fapi
    store.rt["scanner_status"] = "正在扫描..."
    store.rt["scan_start_timestamp"] = int(time.time())
    st = time.time()
    try:
        fapi.set_api_keys(store.api["key"], store.api["secret"])
        tickers = {t["symbol"]: t for t in fapi.all_tickers()}
        pool = _candidates(tickers)
        store.rt["candidate_pool"] = pool
        # 真实账户资产 + 持仓
        if not fapi._dry_run:
            acct = fapi.account()
            store.rt["account_total_assets"] = float(acct.get("totalMarginBalance", 0))
            store.rt["available_margin"] = float(acct.get("availableBalance", 0))
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
            store.rt["positions"] = pos
        # 自动交易：尝试对高优先级候选开仓
        if store.cfg["auto_trade_enabled"] and not fapi._dry_run:
            opened = []
            for cand in pool[:3]:
                sym0 = cand["symbol"].replace("/", "")
                side = "SELL" if cand["direction"] == "SHORT" else "BUY"
                price = cand["current_price"]
                qty = store.cfg["open_margin"] * store.cfg["leverage"] / price if price > 0 else 0
                if qty <= 0:
                    continue
                try:
                    fapi.set_leverage(sym0, store.cfg["leverage"])
                    fapi.new_order(sym0, side, qty)
                    opened.append(cand["symbol"])
                except BinanceError as e:
                    print(f"[auto-trade] {sym0} 开仓失败: {e}")
            if opened:
                print(f"[auto-trade] 开仓 {opened}")
        store.rt["scanner_status"] = "⏳ 倒计时"
        store.rt["last_scan_duration"] = round(time.time() - st, 1)
        store.rt["next_scan_timestamp"] = time.time() + 60
    except BinanceError as e:
        print(f"[scan] FAPI错误: {e}")
        store.rt["scanner_status"] = f"行情获取失败"
    except Exception as e:
        print(f"[scan] err: {e}")
        store.rt["scanner_status"] = "扫描异常"


def scan_loop():
    while True:
        try:
            run_scan()
        except Exception as e:
            print("[scan] loop err", e)
        time.sleep(60)


def start_scanner():
    t = threading.Thread(target=scan_loop, daemon=True)
    t.start()


if __name__ == "__main__":
    start_scanner()
    port = int(os.getenv("PORT", "8100"))
    print(f"妖币系统后端运行于 http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
