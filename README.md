# 台股開盤前趨勢評分器 V2

這是可部署到 GitHub Pages 的公開唯讀版本。網頁顯示 V2 評分、資料可信度、部位上限、六大模組與市場 K 線。

## 資料更新

GitHub Actions 於台北時間每個工作日約 08:15 執行 `python server.py --refresh-only`，更新 `latest_market_data.json` 與每日快照後部署網頁。GitHub 排程可能延遲；頁面會顯示實際資料產生時間與台股資料日。

## 本機使用

執行 `啟動趨勢評分器.cmd`。本機版可即時呼叫 API；GitHub Pages 版則讀取 Actions 產生的最新 JSON。

本工具僅供研究與風險管理，不構成投資建議。
