#!/usr/bin/env python3
"""auto_research · A股每日自动投研管线

流程：
  1. 先按量化标准从股票池中筛选并排序，选出 N 只 A 股（默认 10 只）：
     抓取全池实时行情 → 硬性标准过滤（排除ST、0<PE≤上限、0<PB≤上限、总市值≥下限）
     → 按“低PE+低PB”综合评分排序 → 取评分前 N 只。（也支持 --select rotate 按日期轮换、--tickers 手动指定）
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


# ---------- 历史K线与技术分析 ----------

def fetch_kline(secid, days=120):
    """抓取日线 OHLCV（新浪 getKLineData，升序）。失败返回 []。"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={secid}&scale=240&ma=no&datalen={days}")
    try:
        raw = _http_get(url, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"})
        data = json.loads(raw.decode("utf-8", "replace") or "[]")
    except Exception:  # noqa: BLE001
        return []
    bars = []
    for d in data:
        try:
            bars.append({"date": d["day"], "open": float(d["open"]), "high": float(d["high"]),
                         "low": float(d["low"]), "close": float(d["close"]),
                         "volume": float(d.get("volume") or 0)})
        except (KeyError, ValueError, TypeError):
            continue
    return bars


def _ema_series(vals, period):
    k = 2.0 / (period + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def _sma(vals, period):
    return sum(vals[-period:]) / period if len(vals) >= period else None


def _rsi(closes, period=14):
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for i in range(-period, 0):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - 100.0 / (1.0 + rs)


def _macd(closes):
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _ema_series(dif, 9)
    hist = [(d - s) * 2 for d, s in zip(dif, dea)]  # 中国习惯：柱=2*(DIF-DEA)
    return dif, dea, hist


def _recent_cross(dif, dea, days=5):
    diff = [d - s for d, s in zip(dif, dea)]
    seg = diff[-(days + 1):]
    cross = "none"
    for i in range(1, len(seg)):
        if seg[i - 1] <= 0 < seg[i]:
            cross = "golden"
        elif seg[i - 1] >= 0 > seg[i]:
            cross = "dead"
    return cross


def _local_extrema(vals, w=3, kind="low"):
    idx = []
    for i in range(w, len(vals) - w):
        window = vals[i - w:i + w + 1]
        if kind == "low" and vals[i] == min(window):
            idx.append(i)
        elif kind == "high" and vals[i] == max(window):
            idx.append(i)
    return idx


def _detect_divergence(closes, dif, lookback=60, w=3):
    """比较最近两个价格波谷/波峰与对应 MACD-DIF，判断底/顶背离。"""
    start = max(0, len(closes) - lookback)
    c, d = closes[start:], dif[start:]
    bottom = top = False
    lows = _local_extrema(c, w, "low")
    highs = _local_extrema(c, w, "high")
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        if c[b] < c[a] and d[b] > d[a]:   # 价创新低、MACD抬高 → 底背离(看多)
            bottom = True
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        if c[b] > c[a] and d[b] < d[a]:   # 价创新高、MACD走低 → 顶背离(看空)
            top = True
    return bottom, top


def technical_analysis(bars):
    """由日线计算 MACD/背离/均线/RSI/动量，并给出 [0,1] 技术评分与信号列表。"""
    closes = [b["close"] for b in bars]
    if len(closes) < 35:   # MACD(26)+DEA(9) 需足够样本
        return None
    dif, dea, hist = _macd(closes)
    cross = _recent_cross(dif, dea, 5)
    bottom, top = _detect_divergence(closes, dif)
    rsi = _rsi(closes, 14)
    ma20, ma60 = _sma(closes, 20), _sma(closes, 60)
    price = closes[-1]
    mom20 = (price / closes[-21] - 1) * 100 if len(closes) >= 21 else None

    score, signals = 0.35, []   # 0.35 中性基线，牛信号加分、熊信号减分
    if bottom:
        score += 0.30; signals.append("MACD底背离(看多)")
    if top:
        score -= 0.30; signals.append("MACD顶背离(看空)")
    if cross == "golden":
        score += 0.20; signals.append("MACD金叉")
    elif cross == "dead":
        score -= 0.15; signals.append("MACD死叉")
    if hist[-1] > 0:
        score += 0.10; signals.append("MACD红柱")
    if ma20 and price > ma20:
        score += 0.10; signals.append("站上MA20")
    if ma20 and ma60 and ma20 > ma60:
        score += 0.10; signals.append("均线多头")
    if rsi is not None:
        if rsi > 80:
            score -= 0.10; signals.append(f"RSI超买({rsi:.0f})")
        elif rsi < 20:
            score += 0.05; signals.append(f"RSI超卖({rsi:.0f})")
    score = max(0.0, min(1.0, score))
    return {"dif": dif[-1], "dea": dea[-1], "hist": hist[-1], "cross": cross,
            "bottom_div": bottom, "top_div": top, "rsi": rsi, "ma20": ma20, "ma60": ma60,
            "price": price, "mom20": mom20, "tech_score": round(score, 4), "signals": signals}


# ---------- 选股（基于实时数据的量化筛选） ----------

DEFAULT_CRITERIA = {
    "pe_min": 0.0,              # PE(TTM) 必须为正（盈利）
    "pe_max": 40.0,            # PE 上限（估值不过高）
    "pb_min": 0.0,             # PB 必须为正
    "pb_max": 10.0,            # PB 上限
    "min_total_mktcap_yi": 300.0,  # 总市值下限（亿元），保证流动性与稳健性
    "exclude_st": True,        # 排除 ST/*ST（退市风险）
    "pe_weight": 0.6,          # 价值分中低 PE 的权重
    "pb_weight": 0.4,          # 价值分中低 PB 的权重
    "tech_weight": 0.4,        # 综合分中技术面的权重（价值面权重=1-该值）
    "kline_days": 120,         # 技术分析所需日线根数
    "require_bullish": False,  # 是否只保留带看多技术信号的个股
}


def _ascending_scores(values):
    """越小越好：返回每个值的 [0,1] 得分（最小值=1.0，最大值=0.0）。"""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    scores = [0.0] * n
    for rank, i in enumerate(order):
        scores[i] = 1.0 - (rank / (n - 1) if n > 1 else 0.0)
    return scores


def screen_universe(universe, criteria, count):
    """先抓全池行情做基本面硬筛，再对通过者补充技术分析，最后按“价值+技术”综合评分排序取前 count。

    返回 (selected, stats)：
      selected：入选个股，每项含 stock/quote/tech/value_score/tech_score/score/rank/reason；
      stats：{"n_universe","n_ok","n_passed","n_bullish","criteria"}，供报告展示。
    """
    rows = [{"stock": stock, "quote": fetch_quote(stock)} for stock in universe]

    passed = []
    for r in rows:
        q = r["quote"]
        if not q.get("ok"):
            continue
        name = (q.get("name") or r["stock"].get("name") or "").upper()
        if criteria["exclude_st"] and "ST" in name:
            continue
        pe, pb, mc = q.get("pe_ttm"), q.get("pb"), q.get("total_mktcap_yi")
        if pe is None or not (criteria["pe_min"] < pe <= criteria["pe_max"]):
            continue
        if pb is None or not (criteria["pb_min"] < pb <= criteria["pb_max"]):
            continue
        if mc is None or mc < criteria["min_total_mktcap_yi"]:
            continue
        passed.append(r)

    # 仅对通过基本面的个股抓日线做技术分析（省请求）
    for r in passed:
        secid = f"{r['stock']['market']}{r['stock']['code']}"
        r["tech"] = technical_analysis(fetch_kline(secid, criteria["kline_days"]))

    n_bullish = sum(1 for r in passed if r.get("tech") and
                    (r["tech"]["bottom_div"] or r["tech"]["cross"] == "golden"
                     or (r["tech"]["ma20"] and r["tech"]["price"] > r["tech"]["ma20"])))

    # 可选：仅保留带看多技术信号者
    ranked_pool = passed
    if criteria.get("require_bullish"):
        ranked_pool = [r for r in passed if r.get("tech") and
                       (r["tech"]["bottom_div"] or r["tech"]["cross"] == "golden"
                        or (r["tech"]["ma20"] and r["tech"]["price"] > r["tech"]["ma20"]))]

    if ranked_pool:
        pe_scores = _ascending_scores([r["quote"]["pe_ttm"] for r in ranked_pool])
        pb_scores = _ascending_scores([r["quote"]["pb"] for r in ranked_pool])
        tw = criteria["tech_weight"]
        for i, r in enumerate(ranked_pool):
            value_score = criteria["pe_weight"] * pe_scores[i] + criteria["pb_weight"] * pb_scores[i]
            tech_score = r["tech"]["tech_score"] if r.get("tech") else 0.35
            r["value_score"] = round(value_score, 4)
            r["tech_score"] = round(tech_score, 4)
            r["score"] = round((1 - tw) * value_score + tw * tech_score, 4)
            tsig = "、".join(r["tech"]["signals"][:2]) if r.get("tech") and r["tech"]["signals"] else "技术面中性"
            r["reason"] = f"低PE({r['quote']['pe_ttm']:.1f})/低PB({r['quote']['pb']:.2f})；{tsig}"
        ranked_pool.sort(key=lambda r: r["score"], reverse=True)

    selected = ranked_pool[:count]
    for rank, r in enumerate(selected, 1):
        r["rank"] = rank
    n_ok = sum(1 for r in rows if r["quote"].get("ok"))
    stats = {"n_universe": len(rows), "n_ok": n_ok, "n_passed": len(passed),
             "n_bullish": n_bullish, "criteria": criteria}
    return selected, stats


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
    '"technical_comment":"结合所给MACD/背离/均线/RSI/动量的技术面解读(无技术数据则填\\"未获取到\\")",'
    '"valuation_label":"低估|合理|高估|数据不足",'
    '"fair_value_range":"合理价格区间(元)或\\"未获取到\\"",'
    '"key_risks":["风险1","风险2"],'
    '"catalysts":["催化剂1"],'
    '"action":"买入|增持|持有|减持|回避",'
    '"confidence":"高|中|低",'
    '"reasoning":"结论依据(2-4句)，需兼顾基本面与技术面",'
    '"dcf_assumptions":null'
    '}'
)


def build_messages(stock, quote, date, tech=None):
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
    if tech:
        facts["技术指标"] = {
            "MACD_DIF": round(tech["dif"], 4), "MACD_DEA": round(tech["dea"], 4),
            "MACD_柱": round(tech["hist"], 4), "MACD交叉": tech["cross"],
            "MACD底背离": tech["bottom_div"], "MACD顶背离": tech["top_div"],
            "RSI14": round(tech["rsi"], 1) if tech["rsi"] is not None else None,
            "MA20": round(tech["ma20"], 2) if tech["ma20"] else None,
            "MA60": round(tech["ma60"], 2) if tech["ma60"] else None,
            "20日动量(%)": round(tech["mom20"], 2) if tech["mom20"] is not None else None,
            "技术信号": tech["signals"],
        }
    user = (
        "以下是该 A 股标的的实时行情、基本面与技术面指标（仅这些为可引用的事实数据）：\n"
        + json.dumps(facts, ensure_ascii=False, indent=2)
        + "\n\n请据此输出一份精炼的个股研究结论，须同时结合基本面与技术面，严格按如下 JSON 结构返回（键名保持一致）：\n"
        + JSON_SCHEMA_HINT
        + "\n\n注意：只可引用上面提供的数字；若数据可用=false，请把 valuation_label 设为\"数据不足\"、fair_value_range 设为\"未获取到\"，"
        "并在 reasoning 中说明因缺少实时数据而降级；若无\"技术指标\"字段，technical_comment 填\"未获取到\"。"
        "dcf_assumptions 仅在你有可辩护的情景假设时给出 dcf.py 兼容对象，否则填 null。"
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


def _tech_badge(tech):
    """技术信号在汇总表中的紧凑标签。"""
    if not tech:
        return "—"
    tags = []
    if tech["bottom_div"]:
        tags.append("底背离▲")
    if tech["top_div"]:
        tags.append("顶背离▼")
    if tech["cross"] == "golden":
        tags.append("金叉")
    elif tech["cross"] == "dead":
        tags.append("死叉")
    if not tags:
        tags.append("红柱" if tech["hist"] > 0 else "绿柱")
    return "/".join(tags)


def render_markdown(date, results, screen_stats=None):
    title = f"auto-equity-research@{date}"
    now = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S %Z")
    sample_desc = ("量化筛选后取评分前 " if screen_stats else "选取 ") + f"{len(results)} 只A股"
    # 注意：不在正文顶部重复页面标题（Notion 页面标题走 page property）。
    lines = []
    lines.append(f"# A股每日投研与投资建议 · {date}")
    lines.append("")
    lines.append(f"> 生成时间：{now}　|　样本：{sample_desc}　|　"
                 "数据源：腾讯/新浪实时行情，估值判断由已配置LLM生成")
    lines.append("")
    lines.append("> **免责声明**：本报告由自动化管线生成，仅为研究参考，**不构成投资建议**；"
                 "定量数据以行情源为准，定性判断可能存在模型误差，请自行核实并独立决策。")
    lines.append("")

    # 选股标准与筛选
    lines.append("## 一、选股标准与筛选")
    lines.append("")
    if screen_stats:
        c = screen_stats["criteria"]
        lines.append("**先按量化标准筛选，再研究**。硬性入选标准：")
        lines.append("")
        lines.append(f"- 排除 ST/*ST 等退市风险标的：{'是' if c['exclude_st'] else '否'}")
        lines.append(f"- 盈利且估值不过高：0 < PE(TTM) ≤ {c['pe_max']:.0f}")
        lines.append(f"- 市净率合理：0 < PB ≤ {c['pb_max']:.0f}")
        lines.append(f"- 规模与流动性：总市值 ≥ {c['min_total_mktcap_yi']:.0f} 亿元")
        lines.append(f"- 行情数据可获取（腾讯/新浪）")
        lines.append("")
        tw = c.get("tech_weight", 0.0)
        lines.append(f"**综合评分 = {1 - tw:.1f}×价值分 + {tw:.1f}×技术分**（降序取前 {len(results)} 只）：")
        lines.append("")
        lines.append(f"- **价值分**：对低 PE、低 PB 做全体百分位打分，加权 `{c['pe_weight']:.1f}×低PE + {c['pb_weight']:.1f}×低PB`，越便宜越高。")
        lines.append("- **技术分**（基于日线，0.35 中性基线上下浮动）：**MACD 底背离**(看多+)/顶背离(看空−)、"
                     "MACD 金叉(+)/死叉(−)、MACD 红柱(+)、站上 MA20(+)、均线多头 MA20>MA60(+)、RSI 超买(−)/超卖(+)。")
        if c.get("require_bullish"):
            lines.append("- 已启用 `--require-bullish`：仅保留带看多技术信号（底背离/金叉/站上MA20）的个股。")
        lines.append("")
        lines.append(f"> 本次：候选池 {screen_stats['n_universe']} 只 → 有效行情 {screen_stats['n_ok']} 只 "
                     f"→ 通过基本面 {screen_stats['n_passed']} 只（带看多技术信号 {screen_stats.get('n_bullish','-')} 只）"
                     f"→ 取综合评分前 {len(results)} 只研究。")
    else:
        lines.append("> 本次未使用量化筛选（手动指定标的或按日期轮换）；技术指标仍会计算并纳入研究。")
    lines.append("")

    # 汇总表
    lines.append("## 二、今日组合速览")
    lines.append("")
    lines.append("| 排名 | 代码 | 名称 | 行业 | 现价(元) | 涨跌幅 | PE(TTM) | PB | 技术信号 | 综合评分 | 估值判断 | 建议动作 | 信心 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        q, a = r["quote"], r["analysis"]
        rank = r.get("rank") if r.get("rank") is not None else "-"
        score = f"{r['score']:.3f}" if r.get("score") is not None else "-"
        lines.append(
            f"| {rank} | {r['code']} | {q.get('name') or r['stock'].get('name')} | {r['stock'].get('sector','-')} "
            f"| {_fmt(q.get('price'))} | {_fmt(q.get('change_pct'),'%')} "
            f"| {_fmt(q.get('pe_ttm'))} | {_fmt(q.get('pb'))} | {_tech_badge(r.get('tech'))} | {score} "
            f"| {a.get('valuation_label','-')} | **{a.get('action','-')}** | {a.get('confidence','-')} |"
        )
    lines.append("")

    # 组合层面建议
    buy = [r for r in results if r["analysis"].get("action") in ("买入", "增持")]
    avoid = [r for r in results if r["analysis"].get("action") in ("减持", "回避")]
    lines.append("## 三、A股投资建议总结")
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
    lines.append("## 四、个股研究详情")
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
        tech = r.get("tech")
        if tech:
            div = "底背离(看多)" if tech["bottom_div"] else ("顶背离(看空)" if tech["top_div"] else "无明显背离")
            cross_cn = {"golden": "金叉", "dead": "死叉", "none": "无交叉"}[tech["cross"]]
            ma_rel = "现价在MA20上方" if (tech["ma20"] and tech["price"] > tech["ma20"]) else "现价在MA20下方"
            lines.append(
                f"- **技术面**：MACD DIF {tech['dif']:.3f}/DEA {tech['dea']:.3f}/柱 {tech['hist']:.3f}（{cross_cn}）；"
                f"{div}；MA20 {_fmt(tech['ma20'])}/MA60 {_fmt(tech['ma60'])}，{ma_rel}；"
                f"RSI14 {_fmt(tech['rsi'], nd=1)}；20日动量 {_fmt(tech['mom20'],'%',1)}；"
                f"技术评分 {tech['tech_score']:.3f}"
                + (f"（信号：{'、'.join(tech['signals'])}）" if tech["signals"] else ""))
        if a.get("technical_comment") and a.get("technical_comment") not in ("未获取到", "-"):
            lines.append(f"- **技术面解读**：{a.get('technical_comment')}")
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
    ap.add_argument("--select", choices=["screen", "rotate"], default="screen",
                    help="选股方式：screen=按实时数据量化筛选(默认)，rotate=按日期轮换")
    ap.add_argument("--universe", default=DEFAULT_UNIVERSE, help="股票池 JSON 路径")
    ap.add_argument("--outdir", default=DEFAULT_OUTDIR, help="输出目录")
    ap.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL"), help="LLM 模型")
    ap.add_argument("--max-tokens", type=int, default=1500)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--no-llm", action="store_true", help="跳过LLM（离线自测，产出占位分析）")
    # 筛选标准（--select screen 时生效）
    ap.add_argument("--pe-max", type=float, default=DEFAULT_CRITERIA["pe_max"], help="PE(TTM) 上限")
    ap.add_argument("--pb-max", type=float, default=DEFAULT_CRITERIA["pb_max"], help="PB 上限")
    ap.add_argument("--min-mktcap", type=float, default=DEFAULT_CRITERIA["min_total_mktcap_yi"],
                    help="总市值下限（亿元）")
    ap.add_argument("--include-st", action="store_true", help="不排除 ST/*ST（默认排除）")
    ap.add_argument("--tech-weight", type=float, default=DEFAULT_CRITERIA["tech_weight"],
                    help="综合评分中技术面权重(0~1)，价值面权重=1-该值，默认0.4")
    ap.add_argument("--require-bullish", action="store_true",
                    help="仅保留带看多技术信号(底背离/金叉/站上MA20)的个股")
    args = ap.parse_args()

    date = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else shanghai_today()

    # 选股
    screen_stats = None
    if args.tickers:
        selected = []
        for t in args.tickers.split(","):
            t = t.strip().lower()
            selected.append({"stock": {"market": t[:2], "code": t[2:], "name": None, "sector": "-"}})
    elif args.select == "screen":
        universe = load_universe(args.universe)
        criteria = dict(DEFAULT_CRITERIA)
        criteria.update({"pe_max": args.pe_max, "pb_max": args.pb_max,
                         "min_total_mktcap_yi": args.min_mktcap, "exclude_st": not args.include_st,
                         "tech_weight": args.tech_weight, "require_bullish": args.require_bullish})
        print(f"[筛选] 基本面标准：0<PE≤{criteria['pe_max']}，0<PB≤{criteria['pb_max']}，"
              f"总市值≥{criteria['min_total_mktcap_yi']}亿，{'排除' if criteria['exclude_st'] else '不排除'}ST；"
              f"技术面权重 {criteria['tech_weight']}（含MACD/背离/均线/RSI）；正在抓取全池行情与日线…")
        selected, screen_stats = screen_universe(universe, criteria, args.count)
        print(f"[筛选] 全池 {screen_stats['n_universe']} 只 → 有效行情 {screen_stats['n_ok']} 只 "
              f"→ 通过基本面 {screen_stats['n_passed']} 只（其中带看多技术信号 {screen_stats['n_bullish']} 只）"
              f"→ 取综合评分前 {len(selected)} 只。")
        if not selected:
            print("[错误] 没有个股通过筛选标准，请放宽 --pe-max/--pb-max/--min-mktcap。", file=sys.stderr)
            sys.exit(2)
    else:
        universe = load_universe(args.universe)
        selected = [{"stock": s} for s in select_tickers(universe, date, args.count)]

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not args.no_llm and not api_key:
        print("[错误] 未设置 OPENROUTER_API_KEY，无法调用LLM。可加 --no-llm 做离线自测。", file=sys.stderr)
        sys.exit(2)
    if not args.no_llm and not args.model:
        print("[错误] 未设置 OPENROUTER_MODEL（或 --model）。", file=sys.stderr)
        sys.exit(2)

    print(f"[信息] 报告日期 {date}，选取 {len(selected)} 只：",
          ", ".join(f"{it['stock']['market']}{it['stock']['code']}" for it in selected))

    results = []
    total_cost = 0.0
    for i, item in enumerate(selected, 1):
        stock = item["stock"]
        code = f"{stock['market'].upper()}{stock['code']}"
        quote = item.get("quote") or fetch_quote(stock)  # 筛选阶段已抓取则复用
        tech = item.get("tech")
        if tech is None and (args.tickers or args.select == "rotate"):
            tech = technical_analysis(fetch_kline(code.lower(), DEFAULT_CRITERIA["kline_days"]))
        print(f"  [{i}/{len(selected)}] {code} {quote.get('name') or ''} "
              f"现价={quote.get('price')} 数据={'OK' if quote.get('ok') else '未获取到'}"
              + (f" 评分={item['score']}" if item.get('score') is not None else "")
              + (f" 技术={'/'.join(tech['signals'][:2]) or '中性'}" if tech else ""))
        if args.no_llm:
            analysis = {
                "one_liner": "（离线自测占位，未调用LLM）",
                "business": "未调用LLM", "fundamentals_comment": "未调用LLM",
                "technical_comment": "未调用LLM",
                "valuation_label": "数据不足" if not quote.get("ok") else "合理",
                "fair_value_range": "未获取到", "key_risks": ["离线自测"],
                "catalysts": [], "action": "持有", "confidence": "低",
                "reasoning": "离线自测模式，仅验证行情抓取与报告渲染。", "dcf_assumptions": None,
            }
            usage = {}
        else:
            try:
                analysis, usage = call_llm(build_messages(stock, quote, date, tech), args.model,
                                           api_key, args.max_tokens, args.timeout)
            except Exception as e:  # noqa: BLE001
                print(f"      LLM失败，降级占位: {e}", file=sys.stderr)
                analysis = {
                    "one_liner": "（LLM调用失败，降级）", "business": "-", "fundamentals_comment": "-",
                    "technical_comment": "-",
                    "valuation_label": "数据不足", "fair_value_range": "未获取到", "key_risks": ["LLM调用失败"],
                    "catalysts": [], "action": "持有", "confidence": "低",
                    "reasoning": f"LLM调用失败：{e}", "dcf_assumptions": None,
                }
                usage = {}
        total_cost += (usage or {}).get("cost", 0) or 0
        dcf = run_dcf_crosscheck(analysis.get("dcf_assumptions"))
        results.append({"stock": stock, "code": code, "quote": quote, "analysis": analysis,
                        "dcf": dcf, "score": item.get("score"), "rank": item.get("rank"),
                        "tech": tech, "value_score": item.get("value_score"),
                        "tech_score": item.get("tech_score")})
        time.sleep(0.3)

    # 排序：按建议动作（买入优先）
    results.sort(key=lambda r: ACTION_ORDER.get(r["analysis"].get("action"), 9))

    title, md = render_markdown(date, results, screen_stats)
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
