# auto-equity-research · A股每日自动投研管线

每日自动**选取 10 只 A 股 → 抓取实时行情 → 由 LLM 生成中文研究结论 → 汇总为一份《A股投资建议》→ 推送到飞书云文档**，标题为 `auto-equity-research@YYYY-MM-DD`。

> **免责声明**：本管线产出仅为研究参考，**不构成投资建议**。定量数据以行情源为准，定性判断可能存在模型误差。

## 组成

| 文件 | 作用 |
|---|---|
| `scripts/auto_research.py` | 主管线：选股 / 取数 / LLM 研究 / DCF 交叉验证 / 生成报告 / 发布飞书 |
| `scripts/a_share_universe.json` | A股候选股票池（沪深大盘蓝筹，约 60 只，覆盖主要行业） |
| `scripts/run_daily.sh` | 每日运行封装（供 crontab 调用） |
| `scripts/dcf.py` | DCF 计算器（被主管线在有情景假设时调用做估值交叉验证） |

## 依赖与配置

- **Python 3**（仅标准库，无第三方依赖）。
- **LLM（必需）**：OpenAI 兼容接口，默认走 [OpenRouter](https://openrouter.ai)。
  - `OPENROUTER_API_KEY`：API Key。
  - `OPENROUTER_MODEL`：模型名（如 `anthropic/claude-3.5-sonnet`、`deepseek/deepseek-chat` 等）。
  - `OPENROUTER_BASE_URL`（可选）：默认 `https://openrouter.ai/api/v1`。
- **飞书发布（必需）**：[lark-cli](https://github.com/larksuite/cli)，需一次性交互式鉴权（见下）。

## 选股逻辑

按**日期确定性轮换**：以日期序号为偏移，从股票池中滑窗取 `--count` 只。同一天结果可复现，不同交易日覆盖不同标的，长期轮转覆盖全池。个股名称、PE、PB、市值等以**实时行情为准**（腾讯行情为主，新浪为备），股票池中的名称仅作离线提示。

## 数据源

- 主：腾讯行情 `qt.gtimg.cn`（现价、涨跌幅、PE(TTM)、PB、换手率、流通/总市值）。
- 备：新浪行情 `hq.sinajs.cn`。
- 两者均失败时，该票标注"未获取到"，LLM 据此降级为"数据不足/持有"。

## 一次性配置飞书 lark-cli 鉴权

`lark-cli` 需要你在浏览器完成一次授权（无法自动完成）：

```bash
export PATH="$HOME/.npm-global/bin:$PATH"
lark-cli config init --new        # 输出授权 URL，在浏览器创建/绑定飞书应用凭证
lark-cli auth login --recommend   # 输出授权 URL，在浏览器完成登录授权
lark-cli auth status              # 确认 ok:true
```

> 提示：文档创建需要文档相关权限（docs 域）。`--recommend` 会自动勾选常用权限；如提示缺少权限，用 `lark-cli auth login --domain docs` 补充授权。

## 使用

```bash
# 生成当日报告到 output/ 并推送飞书
python3 scripts/auto_research.py --publish

# 仅生成不发布（先看效果）
python3 scripts/auto_research.py

# 指定数量 / 日期
python3 scripts/auto_research.py --count 10 --date 2026-08-15 --publish

# 指定标的（覆盖轮换选股）
python3 scripts/auto_research.py --tickers sh600519,sz000858 --publish

# 离线自测（跳过 LLM，仅验证取数与渲染）
python3 scripts/auto_research.py --no-llm --tickers sh600519,sz300750

# 发布到指定飞书文件夹/知识库节点
python3 scripts/auto_research.py --publish --parent-token <folder_or_wiki_token>

# 发布前预览请求（不实际创建）
python3 scripts/auto_research.py --publish-dry-run
```

## 每日定时

用封装脚本 + crontab（示例：每个交易日 18:00 收盘后运行）：

```cron
0 18 * * 1-5 /workspace/scripts/run_daily.sh >> /workspace/output/cron.log 2>&1
```

`run_daily.sh` 支持环境变量：`COUNT`（默认 10）、`PUBLISH`（默认 1，设 0 则只生成不发布）。

> 在 Cursor Cloud Agent 中，也可用平台的定时/自动化 Agent 每日触发 `bash scripts/run_daily.sh`。

## 输出

- 报告文件：`output/auto-equity-research@YYYY-MM-DD.md`。
- 结构：①今日组合速览表（代码/名称/行业/现价/涨跌/PE/PB/估值判断/建议动作/信心）→ ②A股投资建议总结（买入/增持、规避、观望分组）→ ③个股研究详情（速览、业务、指标、基本面解读、估值、风险、催化、结论）。
- 发布成功后在飞书生成同名 Markdown 文档。
