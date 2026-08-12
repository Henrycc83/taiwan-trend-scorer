const fs = require('node:fs');
const path = require('node:path');
const { score } = require('./scoring.js');

const snapshotDir = path.join(__dirname, 'snapshots');
const files = fs.existsSync(snapshotDir) ? fs.readdirSync(snapshotDir).filter(x => x.endsWith('.json')) : [];
const byDay = new Map();
for (const file of files) {
  try {
    const data = JSON.parse(fs.readFileSync(path.join(snapshotDir, file), 'utf8'));
    if (data.model_version !== '2.0' || !data.trading_day || !data.market?.taiex?.close) continue;
    byDay.set(data.trading_day, { day: data.trading_day, close: data.market.taiex.close, result: score(data.input || {}) });
  } catch (_) {}
}

const rows = [...byDay.values()].sort((a, b) => a.day.localeCompare(b.day));
const pct = (a, b) => (b / a - 1) * 100;
const evaluated = rows.map((row, i) => ({
  ...row,
  forward5: rows[i + 5] ? pct(row.close, rows[i + 5].close) : null,
  forward20: rows[i + 20] ? pct(row.close, rows[i + 20].close) : null
}));

const groups = [
  ['0–34', 0, 35], ['35–54', 35, 55], ['55–74', 55, 75], ['75–100', 75, 101]
].map(([label, low, high]) => {
  const members = evaluated.filter(x => x.result.total >= low && x.result.total < high);
  const avg = key => {
    const values = members.map(x => x[key]).filter(Number.isFinite);
    return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
  };
  return { label, observations: members.length, averageForward5: avg('forward5'), averageForward20: avg('forward20') };
});

console.log(JSON.stringify({
  modelVersion: '2.0',
  snapshotCount: rows.length,
  ready: rows.length >= 25,
  message: rows.length >= 25 ? '可進行初步紙上分組檢查；正式校準仍需更長走勢外樣本。' : `目前${rows.length}筆，至少累積25筆才開始檢查，建議20日評估需40筆以上。`,
  groups
}, null, 2));
