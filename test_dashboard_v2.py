import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
CACHE = (ROOT / "latest_market_data.json").read_text(encoding="utf-8")


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1200})
    console_errors = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.route("**/api/refresh", lambda route: route.fulfill(status=200, content_type="application/json; charset=utf-8", body=CACHE))
    page.goto("http://127.0.0.1:8765/index.html", wait_until="networkidle")
    page.wait_for_function("document.querySelector('#total').textContent !== '0.0'")

    assert "V2" in page.locator(".eyebrow").inner_text()
    assert page.locator("#confidence").inner_text() == "資料可信度 100%"
    assert page.locator("#limit").inner_text() == "持股上限 30%"
    assert page.get_by_text("納指", exact=True).count() == 1
    assert page.get_by_text("全球科技風險", exact=True).count() >= 1
    assert page.get_by_text("期貨與法人部位", exact=True).count() >= 1
    assert page.locator("#chart-sox").count() == 1
    assert page.locator("#chart-futuresNight").count() == 1

    page.locator("#taiexRet20").fill("")
    assert page.locator("#status").inner_text() == "資料不足"
    assert page.locator("#limit").inner_text() == "持股上限 不提供"
    page.locator("#taiexRet20").fill(str(json.loads(CACHE)["input"]["taiexRet20"]))
    page.get_by_text("06 波動與破壞訊號", exact=True).click()
    page.locator("#atrPercentile").fill("95")
    assert "ATR" in page.locator("#gateBox").inner_text()

    page.reload(wait_until="networkidle")
    page.wait_for_function("document.querySelector('#confidence').textContent === '資料可信度 100%'")
    page.screenshot(path=str(ROOT / "dashboard_v2.png"), full_page=True)

    page.unroute("**/api/refresh")
    page.goto("http://127.0.0.1:8765/index.html?static=1", wait_until="networkidle")
    page.wait_for_function("document.querySelector('#confidence').textContent === '資料可信度 100%'")
    assert page.locator("#refresh").inner_text() == "重新讀取最新資料"
    assert "GitHub 資料更新時間" in page.locator("#apiStatus").inner_text()
    assert not console_errors, console_errors
    browser.close()

print("V2 dashboard browser tests passed")
