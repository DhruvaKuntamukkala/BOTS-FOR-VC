import asyncio
import os
from typing import List, Optional

from playwright.async_api import async_playwright
from backend.bots.base_bot import BaseBot
from backend.bots.speaker_hook import SPEAKER_HOOK_JS
from backend.bots.speaker_debug import dump_speakers

_FINGERPRINT_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {} };
"""


_PARTICIPANT_JS = """() => {
    function isName(s) {
        if (!s || s.length < 2 || s.length > 60) return false;
        if (/^[a-z(]/.test(s)) return false;
        const BAD = /^(unmute|mute|video|stop|start|share|chat|react|more|leave|end|raise|lower|hand|pin|spotlight|rename|remove|host|co-host|waiting|lobby|admit|participants|security|record|caption|whiteboard|apps|webex|cisco|settings|reactions|present|invite|everyone|you$|guest|audio|speaker|unknown|camera|microphone|view|mindx)/i;
        if (BAD.test(s)) return false;
        if (/[0-9]+[.][0-9]+/.test(s)) return false;
        const words = s.trim().split(/[ \t]+/);
        if (words.length > 5) return false;
        if (words.length >= 4) {
            const h = Math.floor(words.length / 2);
            if (words.slice(0, h).join(' ') === words.slice(h).join(' ')) return false;
        }
        return true;
    }
    const found = new Set();
    // Strategy 1: Webex-specific selectors
    const WEBEX_SELECTORS = [
        '[class*="participant-name"]',
        '[class*="display-name"]',
        '[class*="nameplate"]',
        '[class*="name-label"]',
        '[class*="roster"] [class*="name"]',
        '[class*="attendee"] [class*="name"]',
        '[data-testid*="name"]',
        '[class*="video-name"]',
    ];
    for (const sel of WEBEX_SELECTORS) {
        try {
            for (const el of document.querySelectorAll(sel)) {
                const t = (el.innerText || el.textContent || '').split('\n')[0].trim();
                if (isName(t)) found.add(t);
            }
        } catch(e) {}
    }
    // Strategy 2: Iterative tree walk (avoids stack overflow on deep Vue/React DOMs)
    try {
        const stack = [document.body];
        while (stack.length) {
            const node = stack.pop();
            if (!node) continue;
            if (node.nodeType === Node.TEXT_NODE) {
                const t = node.textContent.trim();
                if (isName(t)) found.add(t);
            } else {
                if (node.shadowRoot) stack.push(node.shadowRoot);
                for (let i = node.childNodes.length - 1; i >= 0; i--)
                    stack.push(node.childNodes[i]);
            }
        }
    } catch(e) {}
    return [...found];
}"""


class WebexSDKBot(BaseBot):
    def __init__(self, url: str, use_rtms: bool = False):
        super().__init__(url, use_rtms)
        self.browser = None
        self.context = None
        self.page = None
        self._playwright = None
        self.participants = []
        self._participant_task: Optional[asyncio.Task] = None
        self._seen_participants: set = set()

    async def _fast_click(self, texts: list) -> bool:
        for text in texts:
            for frame in self.page.frames:
                try:
                    loc = frame.get_by_text(text, exact=False)
                    if await loc.count() > 0 and await loc.first.is_visible(timeout=1000):
                        await loc.first.click()
                        print(f"[WebexSDKBot] Clicked: '{text}'")
                        return True
                except Exception:
                    pass
        return False

    async def _poll_participants(self):
        await asyncio.sleep(3)
        while True:
            try:
                for frame in self.page.frames:
                    try:
                        names = await frame.evaluate(_PARTICIPANT_JS)
                        if names:
                            print(f"[WebexSDKBot] Poll found names: {names}")
                        for n in (names or []):
                            self._seen_participants.add(n)
                    except Exception:
                        pass
                if self._seen_participants:
                    await self._push_names_to_hook(self.page, list(self._seen_participants))
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(3)

    async def _delayed_debug_dump(self):
        await asyncio.sleep(30)
        try:
            await dump_speakers(self.page, "webex", "in_meeting")
        except Exception:
            pass

    async def start(self):
        print(f"[WebexSDKBot] Starting browser for {self.url}")
        os.makedirs("logs", exist_ok=True)

        self._playwright = await async_playwright().start()

        args = [
            "--use-fake-ui-for-media-stream",
            "--disable-blink-features=AutomationControlled",
            "--autoplay-policy=no-user-gesture-required",
            "--no-first-run",
        ]
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"

        self.browser = await self._playwright.chromium.launch(headless=False, args=args)
        self.context = await self.browser.new_context(
            user_agent=ua,
            viewport={"width": 1280, "height": 720},
            permissions=["camera", "microphone"],
        )
        self.page = await self.context.new_page()
        await self.page.add_init_script(SPEAKER_HOOK_JS)
        await self.page.add_init_script(_FINGERPRINT_JS)

        await self.page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        await self.page.screenshot(path=f"logs/webex_01_landed.png")

        # Accept cookies if the consent banner is visible
        await self._fast_click(["Accept", "Accept all", "Accept All"])
        await asyncio.sleep(1)

        # Step 1: Close any open dialogs by clicking their close buttons.
        # (Do this BEFORE CSS injection so Vue registers the close action.)
        for frame in self.page.frames:
            try:
                close_btns = await frame.query_selector_all(
                    'button.el-dialog__headerbtn, button[aria-label="el.dialog.close"]'
                )
                for btn in close_btns:
                    try:
                        if await btn.is_visible():
                            await btn.click()
                            await asyncio.sleep(0.4)
                    except Exception:
                        pass
            except Exception:
                pass
        await asyncio.sleep(1)

        # Step 2: Inject CSS to keep dialog overlays hidden after they close.
        # Do NOT hide converge_tip — that is the main page container with the
        # "Join from browser" link inside it.
        await self.page.add_style_tag(content="""
            .el-dialog__wrapper,
            .v-modal { display: none !important; pointer-events: none !important; }
        """)
        print("[WebexSDKBot] Injected CSS to suppress dialog re-renders.")
        await asyncio.sleep(1)
        await self.page.screenshot(path=f"logs/webex_02_no_dialogs.png")

        # Step 3: Find and click "Join from browser" — try multiple text variants
        clicked = await self._fast_click([
            "Join from this browser",
            "Join from browser",
            "Join from your browser",
            "Join in browser",
            "Join as a guest",
            "Open in browser",
        ])
        if not clicked:
            # Fallback: find any <a> or button linking to the meeting in browser
            try:
                el = await self.page.query_selector('a[href*="browser"], a[href*="join"]')
                if el:
                    await el.click()
                    clicked = True
                    print("[WebexSDKBot] Clicked browser join link via href.")
            except Exception:
                pass
        if clicked:
            print("[WebexSDKBot] Clicked 'Join from browser'.")
        else:
            print("[WebexSDKBot] No 'Join from browser' button found — may already be on join screen.")

        # Step 4: Wait for the meeting pre-join UI to load (name field or Join button)
        print("[WebexSDKBot] Waiting for meeting UI to load...")
        try:
            await self.page.wait_for_selector(
                'input[type="text"], input[aria-label*="name" i], '
                'input[placeholder*="name" i], button:has-text("Join")',
                timeout=30000,
            )
            print("[WebexSDKBot] Meeting UI loaded.")
        except Exception:
            print("[WebexSDKBot] Meeting UI not detected in 30s — proceeding anyway.")
        await self.page.screenshot(path=f"logs/webex_03_prejoin.png")

        await self._fast_click(["Continue without microphone and camera", "Continue without microphone"])
        await asyncio.sleep(2)

        for frame in self.page.frames:
            try:
                inp = frame.locator('input[aria-label*="Name" i], input[placeholder*="name" i], input[type="text"]').first
                if await inp.is_visible(timeout=2000):
                    await inp.click()
                    await self.page.keyboard.press("Control+a")
                    await self.page.keyboard.press("Delete")
                    await inp.type(self.bot_name, delay=60)
                    print(f"[WebexSDKBot] Name entered.")
                    break
            except Exception:
                pass

        await asyncio.sleep(1)

        self._participant_task = asyncio.create_task(self._poll_participants())
        asyncio.create_task(self._delayed_debug_dump())

        await self._fast_click(["Join meeting", "Join Meeting", "Join"])
        await asyncio.sleep(5)
        print("[WebexSDKBot] Join flow complete.")

        # Start tracking after clicking join and waiting to avoid lobby ghost speakers
        self._start_tracking(self.page, platform='webex')

    async def stop(self) -> List[str]:
        print("[WebexSDKBot] Stopping...")

        await self._stop_tracking(self.page)

        # Final scrape BEFORE cancelling task
        try:
            if self.page and not self.page.is_closed():
                for frame in self.page.frames:
                    try:
                        names = await frame.evaluate(_PARTICIPANT_JS)
                        if names:
                            print(f"[WebexSDKBot] Final scrape found: {names}")
                        for n in (names or []):
                            self._seen_participants.add(n)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[WebexSDKBot] Final scrape error: {e}")

        if self._participant_task and not self._participant_task.done():
            self._participant_task.cancel()
            try:
                await self._participant_task
            except asyncio.CancelledError:
                pass

        try:
            if self.page and not self.page.is_closed():
                for frame in self.page.frames:
                    try:
                        names = await frame.evaluate(_PARTICIPANT_JS)
                        for n in (names or []):
                            self._seen_participants.add(n)
                    except Exception:
                        pass
        except Exception:
            pass

        self.participants = sorted(self._seen_participants)

        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()

        return self.participants
