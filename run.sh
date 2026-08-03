#!/usr/bin/env bash
# 妖币交易系统后端（重建版）启动脚本
cd "$(dirname "$0")"
# 需要 flask 时首次执行: python3 -m venv .venv && .venv/bin/pip install flask
# 子路径部署(nginx 反代到 /yaob/): 保持 YAOB_BASE=/yaob
export YAOB_BASE="${YAOB_BASE:-/yaob}"
exec .venv/bin/python app.py
