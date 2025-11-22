import asyncio
import json
from playwright.async_api import async_playwright

TARGETS = json.loads('''$targets_json''')
BASE_URL = "$base_url"

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        try:
            for target in TARGETS:
                url = BASE_URL + target["path"]
                try:
                    await page.goto(url, timeout=15000)
                    await page.wait_for_timeout(2000)
                except Exception as nav_err:
                    print(f"NAV_ERROR|{target['label']}|{nav_err}")
                    continue

                full_path = f"/home/user/{target['safe_label']}_full.png"
                await page.screenshot(path=full_path, full_page=True)
                print(f"FULL_SUCCESS|{target['label']}|{full_path}")

                selector = target.get("selector") or "body"
                try:
                    locator = page.locator(selector)
                    if await locator.count() > 0:
                        partial_path = f"/home/user/{target['safe_label']}_partial.png"
                        await locator.first.screenshot(path=partial_path)
                        print(f"PARTIAL_SUCCESS|{target['label']}|{partial_path}")
                    else:
                        print(f"SELECTOR_NOT_FOUND|{target['label']}")
                except Exception as sel_err:
                    print(f"PARTIAL_FAIL|{target['label']}|{sel_err}")
        finally:
            await browser.close()

asyncio.run(run())

