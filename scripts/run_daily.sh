#!/usr/bin/env bash
# auto-equity-research 每日运行封装：生成当日 A 股投研报告 Markdown 到 output/。
# 发布到 Notion 由 Notion MCP 完成（见 scripts/AUTO_RESEARCH.md）：由按日触发的 Cloud Agent
# 运行本脚本后，将生成的 Markdown 通过 notion-create-pages 写入 equity_research_report 容器页。
#
# 依赖环境变量：OPENROUTER_API_KEY、OPENROUTER_MODEL（LLM）。
#
# 手动运行：       bash scripts/run_daily.sh
# 指定数量：       COUNT=10 bash scripts/run_daily.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

COUNT="${COUNT:-10}"

echo "[$(date '+%F %T')] 开始运行 auto_research（count=$COUNT）"
python3 scripts/auto_research.py --count "$COUNT"
echo "[$(date '+%F %T')] 完成（Markdown 已生成，待 Notion MCP 发布）"
