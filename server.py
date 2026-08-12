from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "latest_market_data.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TrendScorer/2.0"

SOURCES = {
    "twse": "https://www.twse.com.tw/",
    "tpex": "https://www.tpex.org.tw/openapi/",
    "taifex": "https://www.taifex.com.tw/",
    "finmind": "https://finmindtrade.com/analysis/#/data/api",
    "yahoo": "https://finance.yahoo.com/quote/%5ETWOII/history/",
    "sox": "https://historyofmarket.com/semi/semi-price/",
    "sox_check": "https://indexes.nasdaq.com/Index/Overview/SOX",
    "nasdaq": "https://www.nasdaq.com/market-activity/index/comp",
}


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "tr": self.row = []
        if tag.lower() in ("td", "th") and self.row is not None:
            self.in_cell, self.cell = True, []

    def handle_data(self, data):
        if self.in_cell: self.cell.append(data)

    def handle_endtag(self, tag):
        if tag.lower() in ("td", "th") and self.in_cell:
            self.row.append(" ".join("".join(self.cell).split()))
            self.in_cell = False
        if tag.lower() == "tr" and self.row is not None:
            if self.row: self.rows.append(self.row)
            self.row = None


def fetch(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except Exception as first_error:
        # Windows Python 3.14 may reject otherwise valid public-site chains when
        # the server omits Subject Key Identifier. curl uses the Windows trust
        # store, so it is a verified fallback rather than disabling TLS checks.
        try:
            completed = subprocess.run(
                ["curl.exe", "-L", "-sS", "--fail", "--max-time", str(timeout), "-A", UA, url],
                capture_output=True, check=True, timeout=timeout + 5,
            )
            return completed.stdout
        except Exception as curl_error:
            raise RuntimeError(f"API連線失敗：{first_error}; curl備援：{curl_error}") from curl_error


def fetch_json(url: str):
    return json.loads(fetch(url).decode("utf-8-sig"))


def clean_number(value):
    if value is None: return None
    text = re.sub(r"[^0-9.\-]", "", str(value).replace(",", ""))
    if not text or not re.fullmatch(r"-?(?:\d+(?:\.\d*)?|\.\d+)", text): return None
    try: return float(text)
    except (TypeError, ValueError): return None


def pct(new, old):
    return round((new / old - 1) * 100, 4) if old else 0.0


def moving_average(rows, size):
    values = [r["close"] for r in rows[-size:]]
    return round(sum(values) / len(values), 2) if len(values) == size else None


def period_return(rows, periods):
    if len(rows) <= periods: return None
    return pct(rows[-1]["close"], rows[-1-periods]["close"])


def field_return(rows, field, periods):
    if len(rows) <= periods or rows[-1].get(field) is None or rows[-1-periods].get(field) is None: return None
    return pct(rows[-1][field], rows[-1-periods][field])


def ma_slope(rows, size=20, lookback=5):
    current = moving_average(rows, size)
    previous = moving_average(rows[:-lookback], size) if len(rows) >= size + lookback else None
    return pct(current, previous) if current is not None and previous is not None else None


def percentile_rank(values, value):
    clean = [v for v in values if v is not None]
    if value is None or not clean: return None
    return round(sum(v <= value for v in clean) / len(clean) * 100, 2)


def risk_metrics(rows):
    complete = [r for r in rows if all(r.get(k) is not None for k in ("high", "low", "close"))]
    if len(complete) < 25: return {"atr_percentile": None, "drawdown20": None}
    true_ranges = []
    for i in range(1, len(complete)):
        row, previous = complete[i], complete[i-1]
        tr = max(row["high"] - row["low"], abs(row["high"] - previous["close"]), abs(row["low"] - previous["close"]))
        true_ranges.append(tr / previous["close"] * 100)
    atr14 = [sum(true_ranges[i-13:i+1]) / 14 for i in range(13, len(true_ranges))]
    current_atr = atr14[-1] if atr14 else None
    recent = complete[-20:]
    drawdown = pct(recent[-1]["close"], max(r["close"] for r in recent))
    return {"atr_percentile": percentile_rank(atr14, current_atr), "drawdown20": drawdown}


def previous_month(first: date, offset: int):
    month = first.month - offset
    year = first.year
    while month <= 0:
        month += 12; year -= 1
    return date(year, month, 1)


def twse_history(today: date):
    found = {}
    for offset in range(5):
        month = previous_month(today.replace(day=1), offset)
        query = month.strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/indicesReport/MI_5MINS_HIST?date={query}&response=json"
        payload = fetch_json(url)
        table = (payload.get("tables") or [{}])[0]
        for row in table.get("data", payload.get("data", [])):
            if len(row) < 5: continue
            roc = re.findall(r"\d+", row[0])
            if len(roc) != 3: continue
            d = f"{int(roc[0])+1911:04d}-{int(roc[1]):02d}-{int(roc[2]):02d}"
            found[d] = {"date": d, "open": clean_number(row[1]), "high": clean_number(row[2]), "low": clean_number(row[3]), "close": clean_number(row[4])}
    return sorted(found.values(), key=lambda x: x["date"])


def tpex_history(today: date):
    found = {}
    # The official TPEx OpenAPI exposes complete OHLC for the current month.
    # Yahoo is used only to backfill older months, then official rows overwrite
    # overlaps so the newest data remains exchange-sourced.
    try:
        payload = fetch_json("https://query1.finance.yahoo.com/v8/finance/chart/%5ETWOII?range=6mo&interval=1d")
        chart = payload["chart"]["result"][0]; quote = chart["indicators"]["quote"][0]
        for i, stamp in enumerate(chart["timestamp"]):
            values = [quote[k][i] for k in ("open", "high", "low", "close")]
            if any(v is None for v in values): continue
            d = datetime.fromtimestamp(stamp, timezone.utc).date()
            if d <= today: found[d.strftime("%Y%m%d")] = {"date": d.isoformat(), "open": values[0], "high": values[1], "low": values[2], "close": values[3]}
    except Exception:
        pass
    rows = fetch_json("https://www.tpex.org.tw/openapi/v1/tpex_index")
    found.update({r["Date"]: {"date": f'{r["Date"][:4]}-{r["Date"][4:6]}-{r["Date"][6:]}', "open": clean_number(r["Open"]), "high": clean_number(r["High"]), "low": clean_number(r["Low"]), "close": clean_number(r["Close"])} for r in rows})
    need = max(0, 25 - len(found))
    if need:
        first = min(datetime.strptime(k, "%Y%m%d").date() for k in found)
        days, cursor = [], first - timedelta(days=1)
        while len(days) < need + 10:
            if cursor.weekday() < 5: days.append(cursor)
            cursor -= timedelta(days=1)
        def one(d):
            ds = d.strftime("%Y/%m/%d")
            url = "https://www.tpex.org.tw/www/zh-tw/afterTrading/indexSummary?" + urllib.parse.urlencode({"date": ds, "response": "json"})
            p = fetch_json(url)
            data = ((p.get("tables") or [{}])[0]).get("data", [])
            if data and data[0][0] == "櫃買指數":
                close = clean_number(data[0][1]); change = clean_number(data[0][2]) or 0
                key = d.strftime("%Y%m%d")
                return key, {"date": d.isoformat(), "open": None, "high": None, "low": None, "close": close, "change": change}
        with ThreadPoolExecutor(max_workers=6) as pool:
            for result in pool.map(one, days):
                if result: found[result[0]] = result[1]
    return sorted(found.values(), key=lambda x: x["date"])


def twse_indices(trading_day: str):
    day = trading_day.replace("-", "")
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={day}&type=IND&response=json"
    payload = fetch_json(url)
    table = (payload.get("tables") or [{}])[0]
    values = {}
    for row in table.get("data", []):
        if len(row) >= 5: values[row[0]] = {"close": clean_number(row[1]), "change_pct": clean_number(row[4])}
    return values


def twse_credit(trading_day: str):
    day = trading_day.replace("-", "")
    url = f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={day}&selectType=ALL&response=json"
    table = (fetch_json(url).get("tables") or [{}])[0]
    data = {row[0]: row for row in table.get("data", []) if row}
    margin = data.get("融資金額(仟元)")
    short = data.get("融券(交易單位)")
    if not margin or not short: raise ValueError("證交所信用交易彙總欄位缺失")
    return {
        "margin_previous": clean_number(margin[-2]), "margin_current": clean_number(margin[-1]),
        "margin_pct": pct(clean_number(margin[-1]), clean_number(margin[-2])),
        "short_previous": clean_number(short[-2]), "short_current": clean_number(short[-1]),
        "short_pct": pct(clean_number(short[-1]), clean_number(short[-2])),
    }


def credit_history(today: date):
    start = today - timedelta(days=75)
    url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode({"dataset": "TaiwanStockTotalMarginPurchaseShortSale", "start_date": start.isoformat(), "end_date": today.isoformat()})
    payload = fetch_json(url)
    if payload.get("status") != 200: raise ValueError(payload.get("msg") or "FinMind信用交易資料失敗")
    by_date = {}
    for row in payload.get("data", []):
        name = row.get("name")
        if name not in ("MarginPurchaseMoney", "ShortSale"): continue
        item = by_date.setdefault(row["date"], {"date": row["date"]})
        item["margin"] = clean_number(row.get("TodayBalance")) if name == "MarginPurchaseMoney" else item.get("margin")
        item["short"] = clean_number(row.get("TodayBalance")) if name == "ShortSale" else item.get("short")
    return [row for row in sorted(by_date.values(), key=lambda x: x["date"]) if row.get("margin") is not None and row.get("short") is not None]


def html_rows(url):
    parser = TableParser(); parser.feed(fetch(url).decode("utf-8", errors="ignore")); return parser.rows


def taifex_ohlc(day: date, market=1):
    q = day.strftime("%Y/%m/%d")
    url = "https://www.taifex.com.tw/cht/3/futDailyMarketReport?" + urllib.parse.urlencode({"queryType": 2, "marketCode": market, "commodity_id": "TX", "queryDate": q})
    for row in html_rows(url):
        if row and row[0] == "TX" and len(row) >= 6:
            values = [clean_number(row[i]) for i in range(2, 6)]
            if all(v is not None for v in values):
                return {"date": day.isoformat(), "open": values[0], "high": values[1], "low": values[2], "close": values[3]}
    return None


def futures_bundle(today: date, count=60):
    start = today - timedelta(days=130)
    url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode({"dataset": "TaiwanFuturesDaily", "data_id": "TX", "start_date": start.isoformat(), "end_date": today.isoformat()})
    payload = fetch_json(url)
    if payload.get("status") != 200: raise ValueError(payload.get("msg") or "FinMind台指期資料失敗")
    selected, total_oi = {}, {}
    for row in payload.get("data", []):
        if row.get("futures_id") != "TX" or row.get("trading_session") not in ("after_market", "position") or not re.fullmatch(r"\d{6}", str(row.get("contract_date", ""))): continue
        if row.get("trading_session") == "position": total_oi[row["date"]] = total_oi.get(row["date"], 0) + (clean_number(row.get("open_interest")) or 0)
        key = (row["date"], row["trading_session"])
        if key not in selected or (row.get("volume") or 0) > (selected[key].get("volume") or 0): selected[key] = row
    def candle(row): return {"date": row["date"], "open": clean_number(row["open"]), "high": clean_number(row["max"]), "low": clean_number(row["min"]), "close": clean_number(row["close"])}
    night = [candle(row) for (d, session), row in selected.items() if session == "after_market"]
    regular = {}
    for (d, session), row in selected.items():
        if session != "position": continue
        item = candle(row); item["open_interest"] = total_oi.get(d) or None; regular[d] = item
    return {"night": sorted(night, key=lambda x: x["date"])[-count:], "regular": regular}


def futures_prices(trading_day: str):
    base = datetime.strptime(trading_day, "%Y-%m-%d").date()
    def get_price(day, market=0):
        q = day.strftime("%Y/%m/%d")
        url = "https://www.taifex.com.tw/cht/3/futDailyMarketReport?" + urllib.parse.urlencode({"queryType": 2, "marketCode": market, "commodity_id": "TX", "queryDate": q})
        for row in html_rows(url):
            if row and row[0] == "TX" and len(row) >= 6:
                price = clean_number(row[5])
                if price: return price
        raise ValueError(f"期交所市場代碼 {market} 無台指期資料")
    day_price = get_price(base, 0)
    night_price, night_date = day_price, trading_day
    # TAIFEX labels the evening session by the following business date in this
    # report. Search the next few calendar dates to handle weekends/holidays.
    for offset in range(1, 5):
        candidate = base + timedelta(days=offset)
        try:
            value = get_price(candidate, 1)
            if value:
                night_price, night_date = value, candidate.isoformat(); break
        except Exception:
            continue
    return day_price, night_price, night_date


def foreign_futures_net():
    rows = html_rows("https://www.taifex.com.tw/cht/3/futContractsDateExcel")
    current = ""
    for row in rows:
        if "臺股期貨" in row: current = "臺股期貨"
        if current == "臺股期貨" and "外資" in row:
            nums = [clean_number(x) for x in row if clean_number(x) is not None]
            if len(nums) >= 12: return int(nums[-2])
    raise ValueError("期交所外資台指期淨部位解析失敗")


def pc_ratio_history():
    rows = html_rows("https://www.taifex.com.tw/enl/eng3/pcRatio?menuid1=03")
    values = []
    for row in rows:
        if len(row) >= 7 and re.match(r"\d{4}/\d{1,2}/\d{1,2}", row[0]):
            value = clean_number(row[6])
            if value is not None: values.append({"date": row[0], "value": value})
    if values: return values
    raise ValueError("Put/Call Ratio解析失敗")


def pc_ratio():
    latest = pc_ratio_history()[0]
    return latest["value"], latest["date"]


def nasdaq_index_history(symbol: str, today: date, count=60):
    start = today - timedelta(days=130)
    url = f"https://api.nasdaq.com/api/quote/{symbol}/historical?" + urllib.parse.urlencode({"assetclass": "index", "fromdate": start.isoformat(), "todate": today.isoformat(), "limit": count + 15})
    payload = fetch_json(url)
    rows = (((payload.get("data") or {}).get("tradesTable") or {}).get("rows") or [])
    result = []
    for row in rows:
        try: d = datetime.strptime(row["date"], "%m/%d/%Y").date().isoformat()
        except Exception: continue
        values = {k: clean_number(row.get(k)) for k in ("open", "high", "low", "close")}
        if all(v is not None for v in values.values()): result.append({"date": d, **values})
    result = sorted(result, key=lambda x: x["date"])
    # Nasdaq's historical table can lag the official index snapshot by one
    # session. Merge the newer snapshot so scoring never silently misses it.
    try:
        info = (fetch_json(f"https://api.nasdaq.com/api/quote/{symbol}/info?assetclass=index").get("data") or {})
        primary, stats = info.get("primaryData") or {}, info.get("keyStats") or {}
        snap_date = datetime.strptime(primary.get("lastTradeTimestamp", ""), "%b %d, %Y").date().isoformat()
        close = clean_number(primary.get("lastSalePrice")); previous = clean_number((stats.get("previousclose") or {}).get("value"))
        day_range = [clean_number(x) for x in re.findall(r"[0-9,]+(?:\.\d+)?", (stats.get("dayrange") or {}).get("value", ""))]
        if close and previous and len(day_range) >= 2 and (not result or snap_date > result[-1]["date"]):
            result.append({"date": snap_date, "open": previous, "high": max(day_range), "low": min(day_range), "close": close, "snapshot": True})
    except Exception:
        pass
    return result[-count:]


def sox_history(today: date, count=60):
    return nasdaq_index_history("SOX", today, count)


def sox_data(today: date):
    history = sox_history(today)
    if len(history) < 2: raise ValueError("Nasdaq SOX歷史資料不足")
    latest, previous = history[-1], history[-2]
    result = {"date": latest["date"], "close": latest["close"], "previous": previous["close"], "change_pct": pct(latest["close"], previous["close"]), "crosscheck": "未取得", "history": history}
    try:
        series = fetch_json("https://historyofmarket.com/api/semi/price.json")["series"]
        check, check_date = clean_number(series[-1]["close"]), series[-1]["date"]
        nasdaq_same_day = next((row for row in history if row["date"] == check_date), None)
        if check is not None and nasdaq_same_day:
            nasdaq_close = nasdaq_same_day["close"]
            diff = abs(check - nasdaq_close) / nasdaq_close * 100
            result["crosscheck"] = f"{check_date}｜History of Market {check:,.2f}／Nasdaq {nasdaq_close:,.2f}，差異 {diff:.2f}%"
        else:
            check_text = f"{check:,.2f}" if check is not None else "未取得"
            result["crosscheck"] = f"{check_date}｜History of Market {check_text}；Nasdaq 同日資料未取得"
    except Exception: pass
    return result


def nasdaq_composite_data(today: date):
    history = nasdaq_index_history("COMP", today)
    if len(history) < 21: raise ValueError("Nasdaq Composite歷史資料不足")
    latest, previous = history[-1], history[-2]
    return {"date": latest["date"], "close": latest["close"], "previous": previous["close"], "change_pct": pct(latest["close"], previous["close"]), "history": history}


def unclosed_bear_gap(rows):
    complete = [r for r in rows if r.get("high") is not None and r.get("low") is not None]
    for i in range(max(1, len(complete)-20), len(complete)):
        prior, current = complete[i-1], complete[i]
        if current["high"] < prior["low"]:
            later_high = max(r["high"] for r in complete[i:])
            if later_high < prior["low"]: return True
    return False


def collect_market_data():
    errors = []
    today = date.today()
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {
            pool.submit(twse_history, today): "twse_history",
            pool.submit(tpex_history, today): "tpex_history",
            pool.submit(sox_data, today): "sox",
            pool.submit(nasdaq_composite_data, today): "nasdaq",
            pool.submit(futures_bundle, today): "futures_bundle",
            pool.submit(credit_history, today): "credit_history",
        }
        results = {}
        for future in as_completed(jobs):
            name = jobs[future]
            try: results[name] = future.result()
            except Exception as exc: errors.append(f"{name}: {exc}")
    if "twse_history" not in results or "tpex_history" not in results:
        raise RuntimeError("指數歷史資料不足：" + "；".join(errors))
    twse, tpex = results["twse_history"], results["tpex_history"]
    common = sorted(set(r["date"] for r in twse) & set(r["date"] for r in tpex))
    trading_day = common[-1]
    twse = [r for r in twse if r["date"] <= trading_day]
    tpex = [r for r in tpex if r["date"] <= trading_day]
    t, o = twse[-1], tpex[-1]
    indices, credit = {}, {}
    try: indices = twse_indices(trading_day)
    except Exception as exc: errors.append(f"twse_indices: {exc}")
    try: credit = twse_credit(trading_day)
    except Exception as exc: errors.append(f"credit: {exc}")
    futures = {"day": None, "night": None, "night_date": "", "foreign_net": None, "open_interest": None, "foreign_oi_pct": None, "pcr_oi": None, "pcr_date": "", "pcr_percentile": None, "basis_pct": None, "basis_percentile": None}
    bundle = results.get("futures_bundle", {"regular": {}, "night": []})
    try:
        regular, nights = bundle["regular"], bundle["night"]
        futures["day"] = regular[trading_day]["close"]
        futures["open_interest"] = regular[trading_day].get("open_interest")
        candidates = [r for r in nights if r["date"] > trading_day]
        latest_night = candidates[0] if candidates else nights[-1]
        futures["night"], futures["night_date"] = latest_night["close"], latest_night["date"]
    except Exception as exc: errors.append(f"futures_price: {exc}")
    try: futures["foreign_net"] = foreign_futures_net()
    except Exception as exc: errors.append(f"foreign_oi: {exc}")
    try:
        pcr_series = pc_ratio_history(); futures["pcr_oi"], futures["pcr_date"] = pcr_series[0]["value"], pcr_series[0]["date"]
        futures["pcr_percentile"] = percentile_rank([r["value"] for r in pcr_series], futures["pcr_oi"])
    except Exception as exc: errors.append(f"pcr: {exc}")
    if futures["day"] is not None: futures["basis_pct"] = pct(futures["day"], t["close"])
    if futures["foreign_net"] is not None and futures["open_interest"]:
        futures["foreign_oi_pct"] = round(futures["foreign_net"] / futures["open_interest"] * 100, 4)
    try:
        twse_by_date = {r["date"]: r["close"] for r in twse}
        basis_history = [pct(row["close"], twse_by_date[d]) for d, row in bundle["regular"].items() if d in twse_by_date and row.get("close") is not None]
        futures["basis_percentile"] = percentile_rank(basis_history, futures["basis_pct"])
    except Exception as exc: errors.append(f"basis_history: {exc}")
    sox = results.get("sox", {"date": "", "close": 0, "previous": 0, "change_pct": 0, "crosscheck": "取得失敗"})
    if "sox" not in results: errors.append("sox: 費半資料取得失敗")
    nasdaq = results.get("nasdaq", {"date": "", "close": None, "previous": None, "change_pct": None})
    if "nasdaq" not in results: errors.append("nasdaq: 納斯達克綜合指數資料取得失敗")
    t_mas, o_mas = {str(k): moving_average(twse, k) for k in (5,10,20)}, {str(k): moving_average(tpex, k) for k in (5,10,20)}
    price_pct, tpex_price_pct = pct(t["close"], twse[-2]["close"]), pct(o["close"], tpex[-2]["close"])
    taiex_ret5, taiex_ret20 = period_return(twse, 5), period_return(twse, 20)
    tpex_ret5, tpex_ret20 = period_return(tpex, 5), period_return(tpex, 20)
    sox_history_rows, nasdaq_history_rows = sox.get("history", []), nasdaq.get("history", [])
    credit_rows = [r for r in results.get("credit_history", []) if r["date"] <= trading_day]
    margin_ret5, margin_ret20 = field_return(credit_rows, "margin", 5), field_return(credit_rows, "margin", 20)
    short_ret5 = field_return(credit_rows, "short", 5)
    risk = risk_metrics(twse)
    taiex_support = t["close"] >= min(r["close"] for r in twse[-5:])
    tpex_support = o["close"] >= min(r["close"] for r in tpex[-5:])
    input_data = {
        "taiexCloseMa20Pct": pct(t["close"], t_mas["20"]) if t_mas["20"] else None,
        "taiexMa20Slope5": ma_slope(twse), "taiexRet20": taiex_ret20, "taiexSupport": taiex_support,
        "tpexCloseMa20Pct": pct(o["close"], o_mas["20"]) if o_mas["20"] else None,
        "tpexMa20Slope5": ma_slope(tpex), "tpexRet20": tpex_ret20, "tpexSupport": tpex_support,
        "bothUp": price_pct > 0 and tpex_price_pct > 0,
        "breadthPositive": (indices.get("電子工業類指數", {}).get("change_pct") or 0) > 0 and tpex_price_pct > 0,
        "tpexRelative5": (tpex_ret5 - taiex_ret5) if tpex_ret5 is not None and taiex_ret5 is not None else None,
        "tpexRelative20": (tpex_ret20 - taiex_ret20) if tpex_ret20 is not None and taiex_ret20 is not None else None,
        "basisPercentile": futures["basis_percentile"],
        "nightPct": pct(futures["night"], futures["day"]) if futures["night"] is not None and futures["day"] is not None else None,
        "foreignOiPct": futures["foreign_oi_pct"], "pcrPercentile": futures["pcr_percentile"],
        "priceRet5": taiex_ret5, "marginRet5": margin_ret5, "marginRet20": margin_ret20, "shortRet5": short_ret5,
        "soxRet1": sox.get("change_pct"), "soxRet5": period_return(sox_history_rows, 5), "soxRet20": period_return(sox_history_rows, 20),
        "nasdaqRet5": period_return(nasdaq_history_rows, 5), "nasdaqRet20": period_return(nasdaq_history_rows, 20),
        "soxRelative20": (period_return(sox_history_rows, 20) - period_return(nasdaq_history_rows, 20)) if period_return(sox_history_rows, 20) is not None and period_return(nasdaq_history_rows, 20) is not None else None,
        "atrPercentile": risk["atr_percentile"], "drawdown20": risk["drawdown20"],
        "bearGap": unclosed_bear_gap(twse) or unclosed_bear_gap(tpex),
    }
    classic_input = {
        "taiexAboveMa5": t_mas["5"] is not None and t["close"] >= t_mas["5"],
        "taiexAboveMa10": t_mas["10"] is not None and t["close"] >= t_mas["10"],
        "taiexAboveMa20": t_mas["20"] is not None and t["close"] >= t_mas["20"],
        "tpexAboveMa5": o_mas["5"] is not None and o["close"] >= o_mas["5"],
        "tpexAboveMa10": o_mas["10"] is not None and o["close"] >= o_mas["10"],
        "tpexAboveMa20": o_mas["20"] is not None and o["close"] >= o_mas["20"],
        "taiexSupport": taiex_support, "tpexSupport": tpex_support,
        "bothUp": input_data["bothUp"], "breadthPositive": input_data["breadthPositive"],
        "basisPct": futures["basis_pct"], "nightPct": input_data["nightPct"],
        "foreignNet": futures["foreign_net"], "pcrOi": futures["pcr_oi"],
        "pricePct": price_pct, "marginPct": credit.get("margin_pct"), "shortPct": credit.get("short_pct"),
        "soxPct": sox.get("change_pct"), "soxRet5": input_data["soxRet5"],
        "bearGap": input_data["bearGap"],
    }
    feature_values = [v for v in input_data.values() if not isinstance(v, bool)]
    completeness = round(sum(v is not None for v in feature_values) / len(feature_values) * 100, 1) if feature_values else 0
    critical_keys = ["taiexCloseMa20Pct", "taiexMa20Slope5", "taiexRet20", "tpexCloseMa20Pct", "tpexMa20Slope5", "tpexRet20"]
    critical_missing = [key for key in critical_keys if input_data.get(key) is None]
    chart_tpex = [r for r in tpex if all(r.get(k) is not None for k in ("open", "high", "low", "close"))][-60:]
    credit.update({"margin_ret5": margin_ret5, "margin_ret20": margin_ret20, "short_ret5": short_ret5})
    sox.update({"ret5": input_data["soxRet5"], "ret20": input_data["soxRet20"]})
    nasdaq.update({"ret5": input_data["nasdaqRet5"], "ret20": input_data["nasdaqRet20"]})
    output = {"ok": True, "model_version": "1.0 + 2.0", "decision_clock": "台灣時間 08:30 開盤前", "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "trading_day": trading_day, "input": input_data, "classic_input": classic_input,
      "data_quality": {"completeness": completeness, "critical_missing": critical_missing},
      "market": {"taiex": {"close": t["close"], "previous": twse[-2]["close"], "mas": t_mas, "ret5": taiex_ret5, "ret20": taiex_ret20}, "tpex": {"close": o["close"], "previous": tpex[-2]["close"], "mas": o_mas, "ret5": tpex_ret5, "ret20": tpex_ret20}, "futures": futures, "credit": credit, "sox": sox, "nasdaq": nasdaq, "risk": risk},
      "charts": {"sox": sox.get("history", [])[-60:], "taiex": twse[-60:], "tpex": chart_tpex, "futuresNight": results.get("futures_bundle", {}).get("night", [])[-60:]},
      "sources": SOURCES, "errors": errors}
    CACHE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    snapshot_dir = ROOT / "snapshots"; snapshot_dir.mkdir(exist_ok=True)
    (snapshot_dir / f"preopen-{today.isoformat()}.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs): super().__init__(*args, directory=str(ROOT), **kwargs)
    def log_message(self, format, *args): pass
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()
    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path.startswith("/api/refresh"):
            try: self.send_json(collect_market_data())
            except Exception as exc:
                cached = None
                if CACHE.exists():
                    cached = json.loads(CACHE.read_text(encoding="utf-8")); cached["stale"] = True
                self.send_json({"ok": False, "error": str(exc), "cached": cached}, 503); return
        elif self.path.startswith("/api/cached"):
            self.send_json(json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {"ok": False}); return
        super().do_GET()


def free_port(start=8765):
    for port in range(start, start+30):
        with socket.socket() as sock:
            try: sock.bind(("127.0.0.1", port)); return port
            except OSError: continue
    raise RuntimeError("找不到可用的本機連接埠")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="台股開盤前趨勢評分器 V2")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--refresh-only", action="store_true", help="更新 JSON 後結束，供 GitHub Actions 使用")
    args = parser.parse_args()
    if args.refresh_only:
        result = collect_market_data()
        print(json.dumps({"ok": result["ok"], "trading_day": result["trading_day"], "completeness": result["data_quality"]["completeness"]}, ensure_ascii=False))
        raise SystemExit(0)
    port = args.port or free_port(); version = int((ROOT / "index.html").stat().st_mtime); url = f"http://127.0.0.1:{port}/index.html?v={version}"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"台股趨勢評分器已啟動：{url}")
    print("關閉此視窗即可停止程式。")
    if not args.no_browser: threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
