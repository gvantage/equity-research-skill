# auto-equity-research · A股每日自动投研管线

每日自动**选取 10 只 A 股 → 抓取实时行情 → 由 LLM 生成中文研究结论 → 汇总为一份《A股投资建议》→ 发布到 Notion**（`equity_research_report` 容器页下的子页面），标题为 `auto-equity-research@YYYY-MM-DD`。

> **免责声明**：本管线产出仅为研究参考，**不构成投资建议**。定量数据以行情源为准，定性判断可能存在模型误差。

## 组成

| 文件 | 作用 |
|---|---|
| `scripts/auto_research.py` | 选股 / 取数 / LLM 研究 / DCF 交叉验证 / 生成报告 Markdown |
| `scripts/a_share_universe.json` | A股候选股票池（沪深大盘蓝筹，约 60 只，覆盖主要行业） |
| `scripts/run_daily.sh` | 每日运行封装（生成 Markdown） |
| `scripts/dcf.py` | DCF 计算器（被主管线在有情景假设时调用做估值交叉验证） |

**职责分离**：`auto_research.py` 只负责“生成报告 Markdown”；**发布到 Notion 由 Notion MCP 完成**（下文）。这样无需在脚本里存放 Notion Token，直接复用已就绪的 Notion MCP。

## 依赖与配置

- **Python 3**（仅标准库，无第三方依赖）。
- **LLM（必需）**：OpenAI 兼容接口，默认走 [OpenRouter](https://openrouter.ai)。
  - `OPENROUTER_API_KEY`、`OPENROUTER_MODEL`（如 `anthropic/claude-3.5-sonnet`、`deepseek/deepseek-chat`）。
  - `OPENROUTER_BASE_URL`（可选）：默认 `https://openrouter.ai/api/v1`。
- **Notion（必需）**：已配置的 **Notion MCP**（无需额外 Token）。

## 选股逻辑（如何选出 10 只）

默认 `--select screen`：**先按量化标准筛选、打分排序，再取前 10 只研究**。

**第一步：抓全池实时行情**。读取 `a_share_universe.json`（约 60 只沪深大盘蓝筹），逐只抓取腾讯/新浪实时行情。

**第二步：硬性标准过滤**（默认值，均可用命令行覆盖）：

| 标准 | 默认 | 说明 | 覆盖参数 |
|---|---|---|---|
| 排除 ST/*ST | 是 | 规避退市风险（按实时名称含 "ST" 判断） | `--include-st` |
| 盈利且不过高 | `0 < PE(TTM) ≤ 40` | 排除亏损（负PE）与高估 | `--pe-max` |
| 市净率合理 | `0 < PB ≤ 10` | 排除异常/过高 PB | `--pb-max` |
| 规模与流动性 | `总市值 ≥ 300 亿元` | 偏向稳健大盘 | `--min-mktcap` |
| 行情可获取 | — | 腾讯/新浪任一成功 | — |

**第三步：综合评分排序**。对通过筛选的个股，分别按 **低 PE**、**低 PB** 做全体百分位打分（越小得分越高，`[0,1]`），加权 `0.6×低PE + 0.4×低PB` 得综合评分，**降序取前 `--count`（默认 10）只**。评分是“越便宜越靠前”的价值倾向。

个股名称/PE/PB/市值一律**以实时行情为准**（池子里的名称仅作离线提示）。报告顶部“选股标准与筛选”一节会展示本次的标准与漏斗（候选池 → 通过标准 → 入选），组合速览表含**排名**与**综合评分**列。

> 其它选股方式：`--select rotate` 按日期确定性轮换（`offset = (date.toordinal()*count) % N` 环形取 N 只，跨日轮转覆盖全池）；`--tickers sh600519,sz000858` 手动指定标的。用 `--count N` 调整数量。

## 数据源

- 主：腾讯行情 `qt.gtimg.cn`（现价、涨跌幅、PE(TTM)、PB、换手率、流通/总市值）。
- 备：新浪行情 `hq.sinajs.cn`。
- 两者均失败时该票标注“未获取到”，LLM 据此降级为“数据不足/持有”。

## Notion 容器页

已创建容器页 **`equity_research_report`**，每日报告作为其**子页面**：

- 页面：`equity_research_report`
- Page ID：`3bd9aa0b-cdfd-81b3-99c3-c5e5e89d2f91`
- URL：https://app.notion.com/p/3bd9aa0bcdfd81b399c3c5e5e89d2f91

## 每日运行（推荐：按日触发的 Cloud Agent + Notion MCP）

在 Cursor 中新建一个**按日触发的 Cloud Agent（定时/Automation）**，提示词示例：

```
每个交易日执行 A 股每日投研：
1. 运行 `python3 scripts/auto_research.py --count 10` 生成当日报告 Markdown（路径见输出的 [NOTION] markdown_file，标题见 [NOTION] title）。
2. 读取该 Markdown 文件正文，用 Notion MCP 的 notion-create-pages 创建子页面：
   - parent.page_id = 3bd9aa0b-cdfd-81b3-99c3-c5e5e89d2f91（equity_research_report 容器页）
   - properties.title = 上一步的 title（形如 auto-equity-research@YYYY-MM-DD）
   - content = 该 Markdown 正文
3. 回报新建页面的 URL。
```

脚本运行结束会打印：

```
[NOTION] title=auto-equity-research@YYYY-MM-DD
[NOTION] markdown_file=/workspace/output/auto-equity-research@YYYY-MM-DD.md
```

## 使用（本地/手动）

```bash
# 生成当日报告 Markdown 到 output/
python3 scripts/auto_research.py

# 指定数量 / 日期
python3 scripts/auto_research.py --count 10 --date 2026-08-15

# 指定标的（覆盖轮换选股）
python3 scripts/auto_research.py --tickers sh600519,sz000858

# 离线自测（跳过 LLM，仅验证取数与渲染）
python3 scripts/auto_research.py --no-llm --tickers sh600519,sz300750

# 封装脚本
COUNT=10 bash scripts/run_daily.sh
```

## 输出

- 报告文件：`output/auto-equity-research@YYYY-MM-DD.md`（Notion 友好的 Markdown，正文不含页面标题）。
- 结构：①今日组合速览表（代码/名称/行业/现价/涨跌/PE/PB/估值判断/建议动作/信心）→ ②A股投资建议总结（买入/增持、规避、观望分组）→ ③个股研究详情（速览、业务、指标、基本面解读、估值、风险、催化、结论）。
- 发布后在 Notion `equity_research_report` 下生成同名子页面。
