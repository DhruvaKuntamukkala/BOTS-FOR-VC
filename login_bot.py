"""
Run this ONCE to sign your bot into Google.
The session is saved in logs/chrome_profile/ and reused by MeetBot automatically.

Usage:
    python login_bot.py
"""
import sys
import asyncio
import os

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

CHROME_PROFILE_DIR = os.path.abspath("logs/chrome_profile")

async def main():
    from playwright.async_api import async_playwright

    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
    print(f"Opening Chrome with profile: {CHROME_PROFILE_DIR}")
    print("Sign into Google (accounts.google.com) in the browser that opens.")
    print("Once signed in, close the browser window — the session will be saved.")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE_DIR,
            headless=False,
            channel="chrome",   # use real installed Chrome, not Playwright's Chromium
            args=[
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
            ignore_default_args=["--enable-automation"],
        )
        page = await context.new_page()
        await page.goto("https://accounts.google.com")
        print("\nWaiting for you to sign in and close the browser...")
        await context.wait_for_event("close")
        print("Session saved. You can now run the bot normally.")

if __name__ == "__main__":
    asyncio.run(main())
