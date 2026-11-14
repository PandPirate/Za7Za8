#!/usr/bin/env bash
# 先设置 Graph 客户端 ID，再分开发英超、欧冠两封邮件并同步各自的 Outlook 日历。
#
#   bash scripts/send_leagues.sh --dry-run     # 两个预览文件，不发信
#   bash scripts/send_leagues.sh               # 真发两封 + 写两个日历
#   bash scripts/send_leagues.sh --to me@example.com
#
# 其余参数原样传给两个发送入口（--config / --calendar / --apply-calendar 等）。
# 英超或欧冠休赛期没有比赛时，对应的那封会自动跳过（--allow-empty），不报错。
set -euo pipefail

cd "$(dirname "$0")/.."

# 收件人固定写在这里；日常直接 `bash scripts/send_leagues.sh`，不需要传参
TO="glycyrrhizin@outlook.com"

# 公共客户端 ID，不是密钥；外部已设置时保留外部值，方便换应用或临时覆盖
export MAILER_GRAPH_CLIENT_ID="${MAILER_GRAPH_CLIENT_ID:-e28f4134-ef05-48a0-b65c-3dff35cb3dab}"

# cron 的 PATH 通常很短，python 可能叫 python3
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if command -v python >/dev/null 2>&1; then
    PYTHON=python
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
  else
    echo "找不到 python / python3" >&2
    exit 2
  fi
fi

# SMTP 配置优先用 state/mail.json；没有就退回环境变量
CONFIG_ARGS=()
if [ -f state/mail.json ]; then
  CONFIG_ARGS=(--config state/mail.json)
fi


"$PYTHON" scripts/send_digest.py --plugin premier_league --calendar graph --allow-empty --to "$TO" "${CONFIG_ARGS[@]}" "$@"


"$PYTHON" scripts/send_digest.py --plugin champions_league --calendar graph --allow-empty --to "$TO" "${CONFIG_ARGS[@]}" "$@"
