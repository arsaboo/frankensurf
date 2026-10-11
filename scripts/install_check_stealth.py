"""Start Scrapling's and Camoufox's browsers directly, inside the provider
environment `frankensurf setup` creates, and print the full error when one
won't start. Run with that environment's python."""
import asyncio
import traceback

URL = "https://example.com/"


async def main():
    try:
        from scrapling.fetchers import AsyncStealthySession
        async with AsyncStealthySession(headless=True, retries=1) as session:
            page = await session.fetch(URL)
            print("scrapling ok", page.status)
    except Exception:
        print("scrapling FAILED")
        traceback.print_exc()
    try:
        from camoufox.async_api import AsyncCamoufox
        async with AsyncCamoufox(headless=True) as browser:
            page = await browser.new_page()
            await page.goto(URL)
            print("camoufox ok", await page.title())
    except Exception:
        print("camoufox FAILED")
        traceback.print_exc()


asyncio.run(main())
