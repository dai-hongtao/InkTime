#!/usr/bin/env bash
set -euo pipefail

# =========================================================
# InkTime 每日渲染脚本（cron / venv / 防并发）
# =========================================================

# ====== 路径配置（自行修改项目路径和python路径） ======
PROJECT_DIR="/path/to/inktime/InkTime"
VENV_DIR="$PROJECT_DIR/venv"
PYTHON_BIN="$VENV_DIR/bin/python"
LOG_DIR="$PROJECT_DIR/logs"
LOCK_FILE="/tmp/inktime_render.lock"
# =======================================

mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] another render is running, skip." >> "$LOG_DIR/render.log"
  exit 0
fi

echo "[$(date '+%F %T')] render start" >> "$LOG_DIR/render.log"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[$(date '+%F %T')] ERROR: python not found in venv: $PYTHON_BIN" >> "$LOG_DIR/render.log"
  exit 1
fi

if [[ ! -f "config.py" ]]; then
  echo "[$(date '+%F %T')] ERROR: config.py not found in project dir" >> "$LOG_DIR/render.log"
  exit 1
fi

"$PYTHON_BIN" render_daily_photo.py >> "$LOG_DIR/render.log" 2>&1

echo "[$(date '+%F %T')] render done" >> "$LOG_DIR/render.log"