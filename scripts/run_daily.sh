#!/usr/bin/env bash
# auto-equity-research 每日运行封装：生成当日A股投研报告并推送到飞书。
# 依赖环境变量：OPENROUTER_API_KEY、OPENROUTER_MODEL（LLM）；飞书鉴权由 lark-cli 存储。
#
# 手动运行：       bash scripts/run_daily.sh
# 仅生成不发布：   PUBLISH=0 bash scripts/run_daily.sh
# 定时（crontab）：每个交易日 18:00 运行（收盘后）：
#   0 18 * * 1-5 /workspace/scripts/run_daily.sh >> /workspace/output/cron.log 2>&1
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# 让 lark-cli 可用（用户级 npm 全局目录）
export PATH="$HOME/.npm-global/bin:$PATH"
# 加载 nvm（若通过 nvm 安装 node）
# shellcheck disable=SC1090
[ -s "$HOME/.nvm/nvm.sh" ] && source "$HOME/.nvm/nvm.sh" >/dev/null 2>&1 || true

COUNT="${COUNT:-10}"
PUBLISH="${PUBLISH:-1}"

ARGS=(--count "$COUNT")
if [ "$PUBLISH" = "1" ]; then
  ARGS+=(--publish)
fi

echo "[$(date '+%F %T')] 开始运行 auto_research（count=$COUNT publish=$PUBLISH）"
python3 scripts/auto_research.py "${ARGS[@]}"
echo "[$(date '+%F %T')] 完成"
