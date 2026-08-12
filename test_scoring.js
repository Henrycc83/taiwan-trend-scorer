const assert = require('node:assert/strict');
const { score, scoreClassic } = require('./scoring.js');

const empty = score({});
assert.equal(empty.total, 0);
assert.equal(empty.confidence, 0);
assert.equal(empty.actionable, false);
assert.equal(empty.regime.exposure, '不提供');

const bullishInput = {
  taiexCloseMa20Pct:5,taiexMa20Slope5:2,taiexRet20:10,taiexSupport:true,
  tpexCloseMa20Pct:5,tpexMa20Slope5:2,tpexRet20:10,tpexSupport:true,
  bothUp:true,breadthPositive:true,tpexRelative5:3,tpexRelative20:8,
  basisPercentile:100,nightPct:1.5,foreignOiPct:0,pcrPercentile:70,
  priceRet5:5,marginRet5:0,marginRet20:-8,shortRet5:-5,
  soxRet1:3,soxRet5:8,soxRet20:15,nasdaqRet5:6,nasdaqRet20:12,soxRelative20:10,
  atrPercentile:20,drawdown20:0,bearGap:false
};
const bullish = score(bullishInput);
assert.equal(bullish.total, 100);
assert.equal(bullish.confidence, 100);
assert.equal(bullish.regime.exposure, '70%');

const volatilityGate = score({...bullishInput, atrPercentile:95});
assert.equal(volatilityGate.regime.exposure, '30%');
assert.ok(volatilityGate.gates.some(x => x.includes('ATR')));

const missingCritical = score({...bullishInput, taiexRet20:null});
assert.equal(missingCritical.actionable, false);
assert.equal(missingCritical.regime.exposure, '不提供');
assert.ok(missingCritical.total < bullish.total);

const nearA = score({...bullishInput, soxRet1:0.99});
const nearB = score({...bullishInput, soxRet1:1.00});
assert.ok(Math.abs(nearA.total-nearB.total) <= 0.1);

const classicBullishInput = {
  taiexAboveMa5:true,taiexAboveMa10:true,taiexAboveMa20:true,taiexSupport:true,
  tpexAboveMa5:true,tpexAboveMa10:true,tpexAboveMa20:true,tpexSupport:true,
  bothUp:true,breadthPositive:true,basisPct:0.5,nightPct:0.3,foreignNet:1000,pcrOi:100,
  pricePct:1,marginPct:-0.2,shortPct:-0.1,soxPct:1,soxRet5:2,bearGap:false
};
const classicBullish = scoreClassic(classicBullishInput);
assert.equal(classicBullish.total, 100);
assert.equal(classicBullish.regime.exposure, '70%');
assert.equal(scoreClassic({...classicBullishInput, bearGap:true}).gapPenalty, 5);

console.log('Dual-model scoring tests passed');
