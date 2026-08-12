(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TrendScorer = api;
})(typeof self !== "undefined" ? self : this, function () {
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const present = value => value !== "" && value !== null && value !== undefined && Number.isFinite(Number(value));
  const number = value => present(value) ? Number(value) : null;
  const round = value => Math.round(value * 10) / 10;

  function linear(value, low, high, max, reverse = false) {
    const v = number(value);
    if (v === null) return { score: 0, max, available: false };
    let ratio = clamp((v - low) / (high - low), 0, 1);
    if (reverse) ratio = 1 - ratio;
    return { score: ratio * max, max, available: true };
  }

  function booleanSignal(value, max, positive = true) {
    if (value !== true && value !== false) return { score: 0, max, available: false };
    return { score: (value === positive ? max : 0), max, available: true };
  }

  function band(value, start, fullStart, fullEnd, end, max) {
    const v = number(value);
    if (v === null) return { score: 0, max, available: false };
    let ratio = 0;
    if (v >= fullStart && v <= fullEnd) ratio = 1;
    else if (v > start && v < fullStart) ratio = (v - start) / (fullStart - start);
    else if (v > fullEnd && v < end) ratio = (end - v) / (end - fullEnd);
    return { score: clamp(ratio, 0, 1) * max, max, available: true };
  }

  function detail(key, label, signals, note) {
    const score = signals.reduce((sum, item) => sum + item.score, 0);
    const max = signals.reduce((sum, item) => sum + item.max, 0);
    const availableMax = signals.filter(item => item.available).reduce((sum, item) => sum + item.max, 0);
    return { key, label, score: round(score), max, availableMax, complete: availableMax === max, note };
  }

  function baseRegime(total) {
    if (total >= 75) return { name: "積極", exposure: 70, tone: "green", summary: "趨勢與參與度較完整，可順勢參與並維持汰弱留強。" };
    if (total >= 55) return { name: "中性偏多", exposure: 50, tone: "blue", summary: "多方條件占優，但仍需服從風險閘門與資料可信度。" };
    if (total >= 35) return { name: "防守", exposure: 30, tone: "amber", summary: "訊號分歧，保留現金並只持有相對強勢標的。" };
    return { name: "高度防守", exposure: 20, tone: "red", summary: "結構偏空或確認不足，優先降低波動與控制損失。" };
  }

  function classicRegime(total) {
    if (total >= 75) return { name: "積極", exposure: "70%", tone: "green", summary: "均線與市場同步偏多，可順勢持有並持續汰弱留強。" };
    if (total >= 55) return { name: "中性偏多", exposure: "50%", tone: "blue", summary: "多方條件較多，但尚未形成全面共振，採分批布局。" };
    if (total >= 35) return { name: "防守", exposure: "30%", tone: "amber", summary: "均線、籌碼或海外訊號分歧，保留現金等待確認。" };
    return { name: "高度防守", exposure: "20%", tone: "red", summary: "多數技術條件尚未轉強，優先控制部位與損失。" };
  }

  function scoreClassic(input) {
    const flag = (key, points) => ({ score: input[key] === true ? points : 0, max: points, available: input[key] === true || input[key] === false });
    const step = (key, points, test) => {
      const value = number(input[key]);
      return { score: value === null ? 0 : (test(value) ? points : 0), max: points, available: value !== null };
    };
    const group = (key, label, signals, note) => detail(key, label, signals, note);
    const trend = group("classicTrend", "價格與均線趨勢", [
      flag("taiexAboveMa5", 5), flag("taiexAboveMa10", 5), flag("taiexAboveMa20", 5), flag("taiexSupport", 5),
      flag("tpexAboveMa5", 5), flag("tpexAboveMa10", 5), flag("tpexAboveMa20", 5), flag("tpexSupport", 5)
    ], "加權與櫃買各檢查5／10／20日線及近期支撐；每項成立5分。" );
    const sync = group("classicSync", "加權／櫃買同步", [flag("bothUp", 10), flag("breadthPositive", 10)], "兩指數同日上漲10分；電子與中小型廣度為正10分。" );
    const futures = group("classicFutures", "期貨訊號", [
      step("basisPct", 5, v => v >= 0), step("nightPct", 5, v => v >= 0),
      step("foreignNet", 5, v => v >= 0), step("pcrOi", 5, v => v >= 80 && v <= 120)
    ], "正基差、盤後上漲、外資淨部位偏多、P／C OI位於80–120，各5分。" );
    const credit = group("classicCredit", "融資／融券", [
      step("pricePct", 3, v => v >= 0), step("marginPct", 4, v => v <= 0), step("shortPct", 3, v => v <= 0)
    ], "指數上漲3分；融資未增加4分；融券未增加3分。" );
    const sox = group("classicSox", "費半代理", [
      step("soxPct", 6, v => v >= 0), step("soxRet5", 4, v => v >= 0)
    ], "費半前一晚上漲6分，5日動能為正4分。" );
    const details = [trend, sync, futures, credit, sox];
    const availableMax = details.reduce((sum, item) => sum + item.availableMax, 0);
    const raw = details.reduce((sum, item) => sum + item.score, 0) - (input.bearGap === true ? 5 : 0);
    const total = round(clamp(raw, 0, 100));
    const confidence = round(availableMax);
    const regime = confidence < 80
      ? { name: "資料不足", exposure: "不提供", tone: "red", summary: "第一版所需欄位不足，不產生部位建議。" }
      : classicRegime(total);
    return { total, confidence, details, gapPenalty: input.bearGap === true ? 5 : 0, regime };
  }

  function score(input) {
    const v = key => number(input[key]);
    const fmt = (key, digits = 2) => v(key) === null ? "N/A" : v(key).toFixed(digits);

    const trend = detail("trend", "加權／櫃買趨勢結構", [
      linear(input.taiexCloseMa20Pct, -5, 5, 5),
      linear(input.taiexMa20Slope5, -2, 2, 5),
      linear(input.taiexRet20, -10, 10, 5),
      booleanSignal(input.taiexSupport, 2.5),
      linear(input.tpexCloseMa20Pct, -5, 5, 5),
      linear(input.tpexMa20Slope5, -2, 2, 5),
      linear(input.tpexRet20, -10, 10, 5),
      booleanSignal(input.tpexSupport, 2.5)
    ], `加權距20日線 ${fmt("taiexCloseMa20Pct")}%、20日報酬 ${fmt("taiexRet20")}%；櫃買距20日線 ${fmt("tpexCloseMa20Pct")}%、20日報酬 ${fmt("tpexRet20")}%。`);

    const breadth = detail("breadth", "市場廣度與同步", [
      booleanSignal(input.bothUp, 3),
      booleanSignal(input.breadthPositive, 4),
      linear(input.tpexRelative5, -3, 3, 4),
      linear(input.tpexRelative20, -8, 8, 4)
    ], `櫃買相對加權5日 ${fmt("tpexRelative5")}%、20日 ${fmt("tpexRelative20")}%；不重複計入均線分數。`);

    const derivatives = detail("derivatives", "期貨與法人部位", [
      linear(input.basisPercentile, 0, 100, 4),
      linear(input.nightPct, -1.5, 1.5, 3),
      linear(input.foreignOiPct, -80, 0, 4),
      band(input.pcrPercentile, 20, 50, 85, 100, 4)
    ], `基差百分位 ${fmt("basisPercentile", 1)}、盤後 ${fmt("nightPct")}%、外資淨部位／OI ${fmt("foreignOiPct")}%、P/C百分位 ${fmt("pcrPercentile", 1)}。`);

    const price5 = v("priceRet5"), margin5 = v("marginRet5");
    const marginGap = price5 !== null && margin5 !== null ? price5 - margin5 : null;
    const credit = detail("credit", "信用槓桿", [
      linear(marginGap, -5, 5, 4),
      linear(input.marginRet20, -8, 8, 3, true),
      linear(input.shortRet5, -5, 5, 3, true)
    ], `加權5日 ${fmt("priceRet5")}%、融資5日 ${fmt("marginRet5")}%／20日 ${fmt("marginRet20")}%、融券5日 ${fmt("shortRet5")}%。`);

    const global = detail("global", "全球科技風險", [
      linear(input.soxRet1, -3, 3, 2),
      linear(input.soxRet5, -8, 8, 3),
      linear(input.soxRet20, -15, 15, 3),
      linear(input.nasdaqRet5, -6, 6, 2),
      linear(input.nasdaqRet20, -12, 12, 2),
      linear(input.soxRelative20, -10, 10, 3)
    ], `費半1／5／20日 ${fmt("soxRet1")}%／${fmt("soxRet5")}%／${fmt("soxRet20")}%；納指5／20日 ${fmt("nasdaqRet5")}%／${fmt("nasdaqRet20")}%。`);

    const risk = detail("risk", "波動與破壞訊號", [
      linear(input.atrPercentile, 20, 90, 5, true),
      linear(input.drawdown20, -15, 0, 3),
      booleanSignal(input.bearGap, 2, false)
    ], `ATR百分位 ${fmt("atrPercentile", 1)}、20日回撤 ${fmt("drawdown20")}%、未封閉空方缺口：${input.bearGap === true ? "有" : input.bearGap === false ? "無" : "N/A"}。`);

    const details = [trend, breadth, derivatives, credit, global, risk];
    const raw = details.reduce((sum, item) => sum + item.score, 0);
    const availableMax = details.reduce((sum, item) => sum + item.availableMax, 0);
    const total = round(clamp(raw, 0, 100));
    const confidence = round(availableMax);
    const criticalComplete = trend.complete;
    const gates = [];
    let regime = baseRegime(total);
    let cap = regime.exposure;

    if (!criticalComplete || confidence < 80) {
      return { total, raw: total, confidence, actionable: false, details, gates: ["關鍵趨勢資料或整體資料完整度不足"], regime: { name: "資料不足", exposure: "不提供", exposureNumeric: 0, tone: "red", summary: "缺值不給分；關鍵資料補齊前不產生持股建議。" } };
    }
    if (confidence < 90) { cap = Math.min(cap, 30); gates.push("資料可信度低於90%，曝險上限30%"); }
    else if (confidence < 95) { cap = Math.min(cap, 50); gates.push("資料可信度低於95%，曝險上限50%"); }
    if (v("taiexCloseMa20Pct") < 0 && v("tpexCloseMa20Pct") < 0) { cap = Math.min(cap, 50); gates.push("加權與櫃買同時低於20日線，上限50%"); }
    if (v("atrPercentile") !== null && v("atrPercentile") >= 90) { cap = Math.min(cap, 30); gates.push("ATR進入歷史前10%，上限30%"); }
    if (input.bearGap === true && breadth.score < 7.5) { cap = Math.min(cap, 30); gates.push("空方缺口未封閉且市場廣度不足，上限30%"); }
    if (v("drawdown20") !== null && v("drawdown20") <= -10) { cap = Math.min(cap, 30); gates.push("20日回撤超過10%，上限30%"); }

    return { total, raw: total, confidence, actionable: true, details, gates, regime: { ...regime, exposure: `${cap}%`, exposureNumeric: cap, summary: gates.length ? `${regime.summary} 風險閘門：${gates.join("；")}。` : regime.summary } };
  }

  return { score, scoreClassic, regime: baseRegime, classicRegime };
});
