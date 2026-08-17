import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
CACHE = (ROOT / "latest_market_data.json").read_text(encoding="utf-8")
DATA = json.loads(CACHE)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1200})
    console_errors = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.route("**/api/refresh", lambda route: route.fulfill(status=200, content_type="application/json; charset=utf-8", body=CACHE))
    page.goto("http://127.0.0.1:8765/index.html", wait_until="networkidle")
    page.wait_for_function("document.querySelector('#total').textContent !== '0.0'")

    assert "DUAL-MODEL" in page.locator(".eyebrow").inner_text()
    assert page.get_by_text("均線技術分析細項", exact=True).count() == 1
    assert page.get_by_text("TSMOM時間序列動能分析細項", exact=True).count() == 1
    assert page.locator("#classicConfidence").inner_text() == "資料可信度 100%"
    assert page.locator("#confidence").inner_text() == "資料可信度 100%"
    previous_day = DATA["previous_reference"]["trading_day"]
    assert previous_day in page.locator("#classicPrevious").inner_text()
    assert "分" in page.locator("#classicPrevious").inner_text()
    assert previous_day in page.locator("#tsmomPrevious").inner_text()
    assert "分" in page.locator("#tsmomPrevious").inner_text()

    classic_y = page.get_by_text("均線技術分析細項", exact=True).bounding_box()["y"]
    tsmom_y = page.get_by_text("TSMOM時間序列動能分析細項", exact=True).bounding_box()["y"]
    charts_y = page.get_by_text("盤後 K 線型態", exact=True).bounding_box()["y"]
    assert classic_y < tsmom_y < charts_y

    sync_text = page.locator("#syncSignal").inner_text()
    expected = {"golden": "黃金交叉", "death": "死亡交叉", "divergent": "方向分歧"}[DATA["market"]["foreign_fx"]["signal"]]
    assert expected in sync_text
    assert DATA["market"]["foreign_fx"]["as_of"] in sync_text
    assert page.locator("#chart-foreignFx[data-sync='drawn']").count() == 1
    foreign_fx = DATA["market"]["foreign_fx"]
    assert foreign_fx["foreign_as_of"] in page.locator("#latest-foreignFx").inner_text()
    assert foreign_fx["fx_as_of"] in page.locator("#latest-foreignFx").inner_text()
    assert page.locator("#meta-foreignFx").inner_text().startswith("同步判定共同資料至")
    assert page.locator("#chart-foreignFx").get_attribute("data-last-date") == foreign_fx["foreign_as_of"]
    sox = DATA["market"]["sox"]
    assert sox["date"] in page.locator("#latest-sox").inner_text()
    assert f'{sox["close"]:,.2f}' in page.locator("#latest-sox").inner_text()
    assert sox["date"] in page.locator("#meta-sox").inner_text()
    assert page.locator("canvas[data-ma5='drawn'][data-ma20='drawn']").count() == 4

    page.locator("#taiexRet20").fill("")
    assert page.locator("#status").inner_text() == "資料不足"
    page.locator("#taiexRet20").fill(str(DATA["input"]["taiexRet20"]))
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
    assert previous_day in page.locator("#classicPrevious").inner_text()
    assert not console_errors, console_errors
    browser.close()

print("V2 dashboard browser tests passed")
