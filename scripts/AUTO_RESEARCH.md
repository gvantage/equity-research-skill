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

## 选股逻辑（如何选出 10 只）——多因子

默认 `--select screen`：**先多因子打分排序，再取前 10 只研究**。

> 为什么不只用 PE/PB：只按低 PE/PB 选股会掉进**价值陷阱**——便宜往往是因为市场对其成长性/景气度定价悲观，结果只选出一堆"便宜但没人玩"的老银行股。因此本管线把 PE/PB **从硬门槛降级为软因子**，并叠加**动量、板块热度、资金关注度**，让"当前热点/市场重视程度"参与选股。

**第一步：抓全池实时行情**（`a_share_universe.json`，约 89 只，覆盖蓝筹 + 算力/半导体/CXO/军工/消费电子/光伏/传媒/运营商等成长题材）。

**第二步：宽松硬门槛**（仅剔除极端/风险标的，不做价值筛选，默认值可覆盖）：

| 门槛 | 默认 | 覆盖参数 |
|---|---|---|
| 排除 ST/*ST | 是 | `--include-st` |
| 盈利、估值不极端 | `0 < PE(TTM) ≤ 80` | `--pe-max` |
| 市净率不极端 | `0 < PB ≤ 20` | `--pb-max` |
| 规模/流动性 | `总市值 ≥ 150 亿` | `--min-mktcap` |

**第三步（热点注入）：并入当日真实热点**。抓取**同花顺当日强势股题材归因**（`zx.10jqka.com.cn`，零鉴权；灵感来自数据源仓库 [gvantage/a-stock-data](https://github.com/gvantage/a-stock-data)），把当日涨幅居前的 `--seed-hot`（默认 30）只热门标的并入候选池，并对每只标的：①标注**题材归因**（如「CPO散热+液冷服务器」）；②登榜者获得**资金关注度加成**；③以题材首标签作为其板块，参与板块热度计算。据此可统计**今日热门题材 Top**（体现市场在炒什么概念）。东财板块/概念接口在本环境被墙(502)，故用同花顺题材归因作为"热点/概念"信号；数据源失败自动降级（跳过热点注入，不影响主流程）。

**第四步：对通过者抓日线（默认 120 根）计算五个因子**，均在通过池内做 `[0,1]` 百分位打分：

| 因子 | 含义 | 计算 | 默认权重 | 参数 |
|---|---|---|---|---|
| **价值** | 便宜 | `0.6×低PE + 0.4×低PB` 百分位 | 0.20 | `--w-value` |
| **MACD 背离** | 反转/趋势衰竭（**独立高权重**） | 底背离(看多)=1.0 / 顶背离(看空)=0.0 / 无=0.5 | **0.16** | `--w-div` |
| **技术（其余）** | 趋势健康 | MACD 金叉/红柱、站上 MA20、均线多头、RSI（0.35 中性基线浮动） | 0.12 | `--w-tech` |
| **动量/相对强度** | 市场是否追捧 | 20 日与 60 日涨幅（偏重近端） | 0.22 | `--w-momentum` |
| **板块热度** | 市场对板块的重视程度 | 同板块 20 日动量中位数的分位（含同花顺题材板块） | 0.16 | `--w-sector` |
| **资金关注度** | 资金活跃/关注 | 换手率 + 近 5 日放量 + **命中当日同花顺强势股加成** | 0.14 | `--w-heat` |

> **MACD 背离**已从"技术因子内的一个小项"提升为**独立因子**（默认权重 0.16，与价值/动量同一量级）：底背离(看多)满分、顶背离(看空)零分。组合速览表单列 **MACD背离** 列，个股详情的因子分位也单列 MACD背离。`--w-div` 可调高/调低其重要性。

**第五步：综合评分**（权重内部归一化）= 各因子分位的加权和，**降序取前 `--count`（默认 10）只**。报告中登上当日强势股榜的个股以 🔥 标记，并附题材归因；`--seed-hot 0` 可关闭热点注入。

**价值陷阱预警**：价值分高（便宜）但**动量弱且板块遇冷**者，在表格中标 `⚠陷阱`、结论中提示，并把该信号喂给 LLM。

报告顶部“选股标准与筛选”展示权重、**当日板块热度 Top** 与漏斗；组合速览表含 `20日涨幅/技术信号/综合评分` 列；个股详情含 `技术面` 与 `动量与板块热度`（含各因子分位）两行，且这些指标都会喂给 LLM，使结论综合基本面+技术面+动量/板块热度并识别价值陷阱。

> 其它方式：`--select rotate`（按日期轮换）、`--tickers sh600519,sz000858`（手动指定，仍计算全部指标）。用 `--count N` 调整数量，`--require-bullish` 只保留带看多技术信号者。

> 关于"概念/热点"数据源：东方财富概念板块接口在本运行环境不可达（502），故当前以**股票池内同板块动量**近似"板块热度"。若你的环境可访问东财/同花顺概念板块接口（或提供数据源），可把真实"概念热度榜"作为额外因子接入，我可据此扩展。

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
