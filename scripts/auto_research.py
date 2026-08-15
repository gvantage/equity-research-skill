#!/usr/bin/env python3
"""auto_research · A股每日自动投研管线

流程：
  1. 从股票池中按日期确定性轮换选出 N 只 A 股（默认 10 只）。
  2. 抓取实时行情与基础指标（腾讯行情，失败自动降级到新浪；均失败则标注"未获取到"）。
  3. 用已配置的 LLM（OpenRouter，OpenAI 兼容）按 equity-research 纪律逐票生成中文研究结论。
  4. 可选：对 LLM 给出的情景假设调用 scripts/dcf.py 做 DCF 交叉验证（禁止心算）。
  5. 汇总为一份中文《A股投资建议》Markdown（Notion 友好），标题 auto-equity-research@YYYY-MM-DD。

本脚本只负责“生成报告 Markdown”。发布到 Notion 由 Notion MCP 完成（见 scripts/AUTO_RESEARCH.md）：
由一个按日触发的 Cursor Cloud Agent 运行本脚本后，把生成的 Markdown 通过 notion-create-pages
作为子页面写入 Notion 的 equity_research_report 容器页。

用法：
  export OPENROUTER_API_KEY=...            # 必需
  export OPENROUTER_MODEL=...              # 必需（如 anthropic/claude-3.5-sonnet）
  python3 scripts/auto_research.py                 # 生成当日报告到 output/
  python3 scripts/auto_research.py --tickers sh600519,sz000858 --no-llm  # 离线自测（跳过LLM）

免责声明：本脚本产出仅为研究参考，不构成投资建议。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_UNIVERSE = os.path.join(HERE, "a_share_universe.json")
DEFAULT_OUTDIR = os.path.join(os.path.dirname(HERE), "output")
OPENROUTER_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions"
CST = timezone(timedelta(hours=8))  # Asia/Shanghai
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"


# ---------- 日期与选股 ----------

def shanghai_today():
    return datetime.now(CST).date()


def load_universe(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    stocks = data["stocks"] if isinstance(data, dict) else data
    # 稳定排序，保证轮换可复现
    return sorted(stocks, key=lambda s: (s["market"], s["code"]))


def select_tickers(universe, date, count):
    """按日期确定性轮换选股：每天窗口整体后移 count 个，长期覆盖全池且每日不同。"""
    n = len(universe)
    if count >= n:
        return list(universe)
    offset = (date.toordinal() * count) % n
    return [universe[(offset + i) % n] for i in range(count)]


# ---------- 行情抓取 ----------

def _http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read()


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_quote_tencent(secid):
    raw = _http_get(f"https://qt.gtimg.cn/q={secid}",
                    headers={"User-Agent": UA, "Referer": "https://finance.qq.com"})
    txt = raw.decode("gbk", "replace")
    if '"' not in txt:
        return None
    payload = txt.split('"', 2)[1]
    p = payload.split("~")
    if len(p) < 47 or not p[3]:
        return None
    return {
        "name": p[1].replace(" ", ""),
        "price": _to_float(p[3]),
        "prev_close": _to_float(p[4]),
        "open": _to_float(p[5]),
        "change_pct": _to_float(p[32]),
        "high": _to_float(p[33]),
        "low": _to_float(p[34]),
        "amount_wan": _to_float(p[37]),
        "turnover_pct": _to_float(p[38]),
        "pe_ttm": _to_float(p[39]),
        "float_mktcap_yi": _to_float(p[44]),
        "total_mktcap_yi": _to_float(p[45]),
        "pb": _to_float(p[46]),
        "source": "腾讯行情 qt.gtimg.cn",
    }


def fetch_quote_sina(secid):
    raw = _http_get(f"https://hq.sinajs.cn/list={secid}",
                    headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"})
    txt = raw.decode("gbk", "replace")
    if '"' not in txt:
        return None
    payload = txt.split('"', 2)[1]
    p = payload.split(",")
    if len(p) < 4 or not p[3]:
        return None
    price = _to_float(p[3])
    prev = _to_float(p[2])
    change_pct = round((price - prev) / prev * 100, 2) if price and prev else None
    return {
        "name": p[0].replace(" ", ""),
        "price": price,
        "prev_close": prev,
        "open": _to_float(p[1]),
        "change_pct": change_pct,
        "high": _to_float(p[4]),
        "low": _to_float(p[5]),
        "amount_wan": (_to_float(p[9]) / 1e4 if _to_float(p[9]) else None),
        "turnover_pct": None,
        "pe_ttm": None,
        "float_mktcap_yi": None,
        "total_mktcap_yi": None,
        "pb": None,
        "source": "新浪行情 hq.sinajs.cn",
    }


def fetch_quote(stock):
    secid = f"{stock['market']}{stock['code']}"
    for fetcher in (fetch_quote_tencent, fetch_quote_sina):
        try:
            q = fetcher(secid)
            if q and q.get("price"):
                q["ok"] = True
                return q
        except Exception:
            continue
    return {"ok": False, "source": "未获取到", "name": stock.get("name")}


# ---------- LLM 调用 ----------

SYSTEM_PROMPT = (
    "你是一名严谨的中国A股卖方股票分析师，严格遵循 equity-research 研究纪律："
    "①事实与判断分离；②只可引用我在输入中提供的实时行情与指标数字作为定量依据，"
    "严禁编造未提供的财务数据（营收/利润/现金流等具体数值），如需引用未提供数据请写\"未获取到\"；"
    "③公司业务与行业定位可基于公开常识做定性描述，但须标注为定性判断；"
    "④必须给出明确的投资动作结论。全部用简体中文。"
    "你只能输出一个合法的 JSON 对象，不要输出任何额外文字或 markdown 代码块。"
)

JSON_SCHEMA_HINT = (
    '{'
    '"one_liner":"一句话速览",'
    '"business":"主营业务与行业定位(定性)",'
    '"fundamentals_comment":"结合所给PE/PB/换手率/市值等指标的解读",'
    '"valuation_label":"低估|合理|高估|数据不足",'
    '"fair_value_range":"合理价格区间(元)或\\"未获取到\\"",'
    '"key_risks":["风险1","风险2"],'
    '"catalysts":["催化剂1"],'
    '"action":"买入|增持|持有|减持|回避",'
    '"confidence":"高|中|低",'
    '"reasoning":"结论依据(2-4句)",'
    '"dcf_assumptions":null'
    '}'
)


def build_messages(stock, quote, date):
    facts = {
        "日期": str(date),
        "代码": f"{stock['market'].upper()}{stock['code']}",
        "名称": quote.get("name") or stock.get("name"),
        "行业提示": stock.get("sector"),
        "现价(元)": quote.get("price"),
        "涨跌幅(%)": quote.get("change_pct"),
        "市盈率TTM": quote.get("pe_ttm"),
        "市净率PB": quote.get("pb"),
        "换手率(%)": quote.get("turnover_pct"),
        "流通市值(亿元)": quote.get("float_mktcap_yi"),
        "总市值(亿元)": quote.get("total_mktcap_yi"),
        "数据源": quote.get("source"),
        "数据可用": quote.get("ok", False),
    }
    user = (
        "以下是该 A 股标的的实时行情与指标（仅这些为可引用的事实数据）：\n"
        + json.dumps(facts, ensure_ascii=False, indent=2)
        + "\n\n请据此输出一份精炼的个股研究结论，严格按如下 JSON 结构返回（键名保持一致）：\n"
        + JSON_SCHEMA_HINT
        + "\n\n注意：若数据可用=false，请把 valuation_label 设为\"数据不足\"、fair_value_range 设为\"未获取到\"，"
        "并在 reasoning 中说明因缺少实时数据而降级。dcf_assumptions 仅在你有可辩护的情景假设时给出 dcf.py 兼容对象，否则填 null。"
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def _extract_json(text):
    text = text.strip()
    # 去掉可能的 ```json 包裹
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start == -1:
        raise ValueError("响应中未找到 JSON 对象")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("JSON 对象不完整")


def call_llm(messages, model, api_key, max_tokens=1500, timeout=90, retries=3):
    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/gvantage/equity-research-skill",
        "X-Title": "auto-equity-research",
    }
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(OPENROUTER_URL, data=body, headers=headers)
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = json.load(resp)
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return _extract_json(content), usage
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"LLM 调用失败: {last_err}")


# ---------- DCF 交叉验证（调用 dcf.py） ----------

def run_dcf_crosscheck(assumptions):
    """对 LLM 提供的情景假设调用 dcf.py 计算概率加权公允价值，失败返回 None。"""
    if not isinstance(assumptions, dict) or not assumptions.get("scenarios"):
        return None
    try:
        sys.path.insert(0, HERE)
        import dcf  # noqa: WPS433
        shares = assumptions["shares"]
        nd = assumptions.get("net_debt", 0.0)
        wacc, g = assumptions["wacc"], assumptions["terminal_g"]
        weighted, probs = 0.0, 0.0
        rows = []
        for sc in assumptions["scenarios"]:
            r = dcf.dcf_value(sc, wacc, g, shares, nd)
            p = sc.get("prob", 0.0)
            probs += p
            weighted += p * r["per_share"]
            rows.append((sc["name"], p, r["per_share"]))
        if abs(probs - 1.0) > 1e-6:
            return {"ok": False, "note": f"概率和={probs:.2f}≠1，加权值不可用"}
        return {"ok": True, "weighted_per_share": round(weighted, 2), "rows": rows}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "note": f"dcf.py 计算跳过: {e}"}


# ---------- 报告渲染 ----------

ACTION_ORDER = {"买入": 0, "增持": 1, "持有": 2, "减持": 3, "回避": 4}


def _fmt(v, unit="", nd=2):
    if v is None:
        return "未获取到"
    if isinstance(v, float):
        return f"{v:.{nd}f}{unit}"
    return f"{v}{unit}"


def render_markdown(date, results):
    title = f"auto-equity-research@{date}"
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S %Z")
    # 注意：不在正文顶部重复页面标题（Notion 页面标题走 page property）。
    lines = []
    lines.append(f"# A股每日投研与投资建议 · {date}")
    lines.append("")
    lines.append(f"> 生成时间：{now}　|　样本：当日轮换选取 {len(results)} 只A股　|　"
                 "数据源：腾讯/新浪实时行情，估值判断由已配置LLM生成")
    lines.append("")
    lines.append("> **免责声明**：本报告由自动化管线生成，仅为研究参考，**不构成投资建议**；"
                 "定量数据以行情源为准，定性判断可能存在模型误差，请自行核实并独立决策。")
    lines.append("")

    # 汇总表
    lines.append("## 一、今日组合速览")
    lines.append("")
    lines.append("| 代码 | 名称 | 行业 | 现价(元) | 涨跌幅 | PE(TTM) | PB | 估值判断 | 建议动作 | 信心 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        q, a = r["quote"], r["analysis"]
        lines.append(
            f"| {r['code']} | {q.get('name') or r['stock'].get('name')} | {r['stock'].get('sector','-')} "
            f"| {_fmt(q.get('price'))} | {_fmt(q.get('change_pct'),'%')} "
            f"| {_fmt(q.get('pe_ttm'))} | {_fmt(q.get('pb'))} "
            f"| {a.get('valuation_label','-')} | **{a.get('action','-')}** | {a.get('confidence','-')} |"
        )
    lines.append("")

    # 组合层面建议
    buy = [r for r in results if r["analysis"].get("action") in ("买入", "增持")]
    avoid = [r for r in results if r["analysis"].get("action") in ("减持", "回避")]
    lines.append("## 二、A股投资建议总结")
    lines.append("")
    if buy:
        names = "、".join(f"{r['quote'].get('name') or r['code']}({r['code']})" for r in buy)
        lines.append(f"- **相对看好（买入/增持）**：{names}")
    if avoid:
        names = "、".join(f"{r['quote'].get('name') or r['code']}({r['code']})" for r in avoid)
        lines.append(f"- **建议规避（减持/回避）**：{names}")
    hold = len(results) - len(buy) - len(avoid)
    lines.append(f"- 其余 {hold} 只维持**持有/观望**。请结合个人风险偏好与仓位管理执行，注意分散与止损纪律。")
    lines.append("")

    # 个股详情
    lines.append("## 三、个股研究详情")
    lines.append("")
    for idx, r in enumerate(results, 1):
        q, a = r["quote"], r["analysis"]
        lines.append(f"### {idx}. {q.get('name') or r['stock'].get('name')}（{r['code']}）")
        lines.append("")
        lines.append(f"- **一句话速览**：{a.get('one_liner','-')}")
        lines.append(f"- **业务与行业**：{a.get('business','-')}")
        lines.append(f"- **实时指标**：现价 {_fmt(q.get('price'),'元')}，涨跌 {_fmt(q.get('change_pct'),'%')}，"
                     f"PE(TTM) {_fmt(q.get('pe_ttm'))}，PB {_fmt(q.get('pb'))}，"
                     f"总市值 {_fmt(q.get('total_mktcap_yi'),'亿元')}（{q.get('source','-')}）")
        lines.append(f"- **基本面解读**：{a.get('fundamentals_comment','-')}")
        lines.append(f"- **估值判断**：{a.get('valuation_label','-')}；合理区间：{a.get('fair_value_range','未获取到')}")
        risks = a.get("key_risks") or []
        cats = a.get("catalysts") or []
        if risks:
            lines.append(f"- **主要风险**：{'；'.join(map(str, risks))}")
        if cats:
            lines.append(f"- **潜在催化**：{'；'.join(map(str, cats))}")
        dcf = r.get("dcf")
        if dcf and dcf.get("ok"):
            detail = "，".join(f"{n} p={p:.0%}→{ps:.1f}元" for n, p, ps in dcf["rows"])
            lines.append(f"- **DCF交叉验证（基于模型情景假设，dcf.py计算）**："
                         f"概率加权公允价值 **{dcf['weighted_per_share']}元/股**（{detail}）")
        lines.append(f"- **结论**：**{a.get('action','-')}**（信心：{a.get('confidence','-')}）。{a.get('reasoning','')}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*本文件由 scripts/auto_research.py 自动生成。估值方法与纪律参见仓库 SKILL.md 与 references/。*")
    return title, "\n".join(lines)


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser(description="A股每日自动投研管线")
    ap.add_argument("--date", help="报告日期 YYYY-MM-DD，默认今日(Asia/Shanghai)")
    ap.add_argument("--count", type=int, default=10, help="选股数量，默认10")
    ap.add_argument("--tickers", help="覆盖选股，逗号分隔，如 sh600519,sz000858")
    ap.add_argument("--universe", default=DEFAULT_UNIVERSE, help="股票池 JSON 路径")
    ap.add_argument("--outdir", default=DEFAULT_OUTDIR, help="输出目录")
    ap.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL"), help="LLM 模型")
    ap.add_argument("--max-tokens", type=int, default=1500)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--no-llm", action="store_true", help="跳过LLM（离线自测，产出占位分析）")
    args = ap.parse_args()

    date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else shanghai_today()

    # 选股
    if args.tickers:
        selected = []
        for t in args.tickers.split(","):
            t = t.strip().lower()
            selected.append({"market": t[:2], "code": t[2:], "name": None, "sector": "-"})
    else:
        universe = load_universe(args.universe)
        selected = select_tickers(universe, date, args.count)

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not args.no_llm and not api_key:
        print("[错误] 未设置 OPENROUTER_API_KEY，无法调用LLM。可加 --no-llm 做离线自测。", file=sys.stderr)
        sys.exit(2)
    if not args.no_llm and not args.model:
        print("[错误] 未设置 OPENROUTER_MODEL（或 --model）。", file=sys.stderr)
        sys.exit(2)

    print(f"[信息] 报告日期 {date}，选取 {len(selected)} 只：",
          ", ".join(f"{s['market']}{s['code']}" for s in selected))

    results = []
    total_cost = 0.0
    for i, stock in enumerate(selected, 1):
        code = f"{stock['market'].upper()}{stock['code']}"
        quote = fetch_quote(stock)
        print(f"  [{i}/{len(selected)}] {code} {quote.get('name') or ''} "
              f"现价={quote.get('price')} 数据={'OK' if quote.get('ok') else '未获取到'}")
        if args.no_llm:
            analysis = {
                "one_liner": "（离线自测占位，未调用LLM）",
                "business": "未调用LLM", "fundamentals_comment": "未调用LLM",
                "valuation_label": "数据不足" if not quote.get("ok") else "合理",
                "fair_value_range": "未获取到", "key_risks": ["离线自测"],
                "catalysts": [], "action": "持有", "confidence": "低",
                "reasoning": "离线自测模式，仅验证行情抓取与报告渲染。", "dcf_assumptions": None,
            }
            usage = {}
        else:
            try:
                analysis, usage = call_llm(build_messages(stock, quote, date), args.model,
                                           api_key, args.max_tokens, args.timeout)
            except Exception as e:  # noqa: BLE001
                print(f"      LLM失败，降级占位: {e}", file=sys.stderr)
                analysis = {
                    "one_liner": "（LLM调用失败，降级）", "business": "-", "fundamentals_comment": "-",
                    "valuation_label": "数据不足", "fair_value_range": "未获取到", "key_risks": ["LLM调用失败"],
                    "catalysts": [], "action": "持有", "confidence": "低",
                    "reasoning": f"LLM调用失败：{e}", "dcf_assumptions": None,
                }
                usage = {}
        total_cost += (usage or {}).get("cost", 0) or 0
        dcf = run_dcf_crosscheck(analysis.get("dcf_assumptions"))
        results.append({"stock": stock, "code": code, "quote": quote, "analysis": analysis, "dcf": dcf})
        time.sleep(0.3)

    # 排序：按建议动作（买入优先）
    results.sort(key=lambda r: ACTION_ORDER.get(r["analysis"].get("action"), 9))

    title, md = render_markdown(date, results)
    os.makedirs(args.outdir, exist_ok=True)
    out_path = os.path.join(args.outdir, f"{title}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[完成] 报告已生成：{out_path}")
    if total_cost:
        print(f"[信息] LLM 累计成本约 ${total_cost:.4f}")
    # 供发布用（由 Notion MCP 读取）：页面标题与内容文件路径。
    print(f"[NOTION] title={title}")
    print(f"[NOTION] markdown_file={out_path}")
    print("[NOTION] 发布方式：由按日触发的 Cloud Agent 用 Notion MCP notion-create-pages，"
          "parent.page_id=equity_research_report 容器页，properties.title 用上面的 title，content 用该 Markdown 文件正文。")


if __name__ == "__main__":
    main()
