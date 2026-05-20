"""
Zoom SDK Bot
────────────
Join-path priority (highest → lowest):

  1. Zoom Meeting SDK (Web) via Playwright + locally-served zoom_page.html
     • Requires ZOOM_SDK_KEY + ZOOM_SDK_SECRET in config.py
     • Navigates to http://localhost:8000/zoom-sdk
     • Joins with a JWT-signed payload; SDK handles audio/video internally
     • Audio captured via RTCPeerConnection hook → window._mindx.audioChunks
     • Speaker timeline built from onActiveSpeaker SDK events (real diarisation)
     • Falls back to path 2 on any failure

  2. Browser automation (app.zoom.us/wc/join/…)
     • RTCPeerConnection hook injected via add_init_script before page load
     • Audio captured via WebRTC → falls back to WASAPI if hook fails
     • Lobby polling for up to 5 minutes

Both paths share:
  • Lobby handling  — polls every 8 s, waits up to LOBBY_TIMEOUT seconds
  • Session watchdog — checks every WATCHDOG_INTERVAL s; rejoins once on disconnect
  • REST API post-meeting participant list (ZOOM_ACCOUNT_ID / CLIENT_ID / SECRET)
"""

from __future__ import annotations

import asyncio
import glob
import os
import re
import time
from typing import List, Optional

# import requests
from playwright.async_api import BrowserContext, Page, async_playwright

# import config
from backend.audio.webrtc_capture import WEBRTC_HOOK_JS
from backend.bots.base_bot import BaseBot
from backend.bots.speaker_hook import SPEAKER_HOOK_JS
from backend.bots.speaker_debug import dump_speakers



# ── Zoom active-speaker JS ───────────────────────────────────────────────────
# Returns the display name of the person currently speaking, or null.
# Tries five complementary strategies in order of confidence.
_ZOOM_SPEAKER_JS = r"""() => {
    function firstName(el) {
        if (!el) return null;
        const t = (el.innerText || el.textContent || '').trim().split('\n')[0].trim();
        return (t && t.length >= 2 && t.length < 80 && !/^[a-z(]/.test(t)) ? t : null;
    }

    // Signal 1: Participants-panel speaking indicator (fires on actual speech only).
    // Zoom uses one of three class fragments depending on version.
    for (const item of document.querySelectorAll('[class*="participants-item"]')) {
        if (!item.querySelector('[class*="audio-level"],[class*="speaking"],[class*="audio-active"]'))
            continue;
        const n = firstName(item.querySelector('[class*="display-name"],[class*="name"]'));
        if (n) return n;
    }

    // Signal 2: Speaker-active-container — shows who is in the main tile.
    // Falls back to this so the timeline is never completely empty.
    const sac = document.querySelector('[class*="speaker-active-container__wrap"]');
    if (sac) return firstName(sac) || null;

    return null;
}"""

# ── Constants ─────────────────────────────────────────────────────────────────

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_BROWSER_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--disable-blink-features=AutomationControlled",
    "--autoplay-policy=no-user-gesture-required",
    "--no-first-run",
    "--disable-infobars",
]

_FINGERPRINT_SPOOF_JS = """
Object.defineProperty(navigator, 'webdriver',  { get: () => undefined });
Object.defineProperty(navigator, 'plugins',    { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages',  { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {} };
"""


# ── URL helpers ───────────────────────────────────────────────────────────────

def _extract_meeting_id(url: str) -> str:
    for pattern in (r'/j/(\d+)', r'/wc/join/(\d+)', r'/wc/(\d+)'):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return ""


def _extract_password(url: str) -> str:
    m = re.search(r'[?&]pwd=([^&]+)', url)
    return m.group(1) if m else ""


def _build_webclient_url(url: str) -> str:
    """Convert a zoom.us/j/ID URL to the web-client join URL."""
    if "/wc/" in url:
        return url
    m = re.search(r'/j/(\d+)', url)
    if not m:
        return url                       # personal room link — use as-is
    mid    = m.group(1)
    pwd_m  = re.search(r'[?&]pwd=([^&]+)', url)
    suffix = f"?pwd={pwd_m.group(1)}" if pwd_m else ""
    return f"https://app.zoom.us/wc/join/{mid}{suffix}"


def _next_wav_path(prefix: str = "zoom") -> str:
    """Return logs/zoom_N.wav where N is one past the current count."""
    os.makedirs("logs", exist_ok=True)
    n = len(glob.glob(f"logs/{prefix}_*.wav")) + 1
    return f"logs/{prefix}_{n}.wav"


# ── JWT signature ─────────────────────────────────────────────────────────────

# def _generate_sdk_signature(sdk_key: str, sdk_secret: str, meeting_number: str) -> str:
#     iat = int(time.time()) - 30
#     exp = iat + 7_200
#     payload = { "sdkKey": sdk_key, "mn": meeting_number, "role": 0, "iat": iat, "exp": exp, "tokenExp": exp }
#     import jwt
#     token = jwt.encode(payload, sdk_secret, algorithm="HS256")
#     return token if isinstance(token, str) else token.decode()


# ── Bot ───────────────────────────────────────────────────────────────────────

class ZoomSDKBot(BaseBot):
    """
    Zoom recording bot.

    Audio tiers (best → fallback):
      1. SDK path  — WebRTC capture with SDK-based speaker timeline
      2. Browser   — WebRTC capture without per-speaker breakdown
      3. WASAPI    — system loopback, managed by the orchestrator (no action needed)

    When self.handles_audio is True the orchestrator skips its own WASAPI
    recording and uses self.audio_file (set by stop()) instead.
    """

    LOBBY_TIMEOUT     = 300   # seconds before giving up on admission
    WATCHDOG_INTERVAL = 30    # seconds between liveness checks
    MAX_RECONNECTS    = 1     # auto-rejoin attempts before giving up

    def __init__(self, url: str, use_rtms: bool = False) -> None:
        super().__init__(url, use_rtms)

        self._meeting_id = _extract_meeting_id(url)
        self._password   = _extract_password(url)

        # Playwright handles (reset on each join attempt)
        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._page:    Optional[Page]           = None

        # Audio
        self.audio_file: Optional[str]           = None   # set by stop() if WebRTC used
        self.handles_audio: bool                 = False  # tells orchestrator to skip WASAPI

        # Speaker timeline from SDK events (sdk path only)
        # self._sdk_speaker_timeline: List[dict]   = []

        # REST API
        # self._access_token: Optional[str] = None

        # Reconnect
        self._watchdog_task: Optional[asyncio.Task] = None
        self._stop_requested = False
        self._reconnect_count = 0
        self._joined_via = "none"   # 'sdk' | 'browser' | 'none'

        # Participant collection
        self._participant_task:    Optional[asyncio.Task] = None
        self._panel_task:          Optional[asyncio.Task] = None
        self._seen_participants: set = set()

    # ── Config checks ──────────────────────────────────────────────────────────

    # def _rest_configured(self) -> bool:
    #     return bool(
    #         getattr(config, "ZOOM_ACCOUNT_ID",    "")
    #         and getattr(config, "ZOOM_CLIENT_ID",    "")
    #         and getattr(config, "ZOOM_CLIENT_SECRET","")
    #     )

    # def _sdk_configured(self) -> bool:
    #     return bool(
    #         getattr(config, "ZOOM_SDK_KEY",    "")
    #         and getattr(config, "ZOOM_SDK_SECRET", "")
    #     )

    # ── REST API (participant list) ─────────────────────────────────────────────

    # def _get_access_token(self) -> str:
    #     creds = base64.b64encode(
    #         f"{config.ZOOM_CLIENT_ID}:{config.ZOOM_CLIENT_SECRET}".encode()
    #     ).decode()
    #     resp = requests.post(
    #         "https://zoom.us/oauth/token",
    #         params={"grant_type": "account_credentials", "account_id": config.ZOOM_ACCOUNT_ID},
    #         headers={"Authorization": f"Basic {creds}"},
    #         timeout=10,
    #     )
    #     resp.raise_for_status()
    #     return resp.json()["access_token"]

    # def _get_participants_via_api(self) -> List[str]:
    #     if not self._access_token or not self._meeting_id:
    #         return []
    #     try:
    #         resp = requests.get(
    #             f"https://api.zoom.us/v2/report/meetings/{self._meeting_id}/participants",
    #             headers={"Authorization": f"Bearer {self._access_token}"},
    #             timeout=10,
    #         )
    #         if resp.status_code == 200:
    #             return [p["name"] for p in resp.json().get("participants", [])]
    #     except Exception as exc:
    #         print(f"[ZoomSDKBot] Participant API error: {exc}")
    #     return []

    # ── Browser factory ────────────────────────────────────────────────────────

    async def _launch_browser(self) -> None:
        """Tear down any existing instances then launch a fresh browser."""
        await self._cleanup_browser()
        self._playwright = await async_playwright().start()

        profile_dir = os.path.abspath("logs/zoom_chrome_profile")
        os.makedirs(profile_dir, exist_ok=True)

        launch_kwargs = dict(
            user_data_dir=profile_dir,
            headless=False,
            args=_BROWSER_ARGS,
            user_agent=_UA,
            viewport={"width": 1280, "height": 720},
            permissions=["camera", "microphone"],
            ignore_default_args=["--enable-automation"],
        )
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                channel="chrome", **launch_kwargs
            )
            print("[ZoomSDKBot] Launched with real Chrome.")
        except Exception as e:
            print(f"[ZoomSDKBot] Real Chrome unavailable ({e}), using Chromium.")
            self._context = await self._playwright.chromium.launch_persistent_context(
                **launch_kwargs
            )

    async def _cleanup_browser(self) -> None:
        """Close all Playwright resources; reset handles to None."""
        for attr, close_method in (
            ("_page",       lambda o: o.close()),
            ("_context",    lambda o: o.close()),
            ("_playwright", lambda o: o.stop()),
        ):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    await close_method(obj)
                except Exception:
                    pass
                setattr(self, attr, None)

    # ── SDK path ───────────────────────────────────────────────────────────────

    # async def _sdk_join(self) -> None:
    #     ... (sdk join logic)
    # async def _poll_sdk_status(self) -> None:
    #     ... (sdk status polling)

    # ── Browser fallback path ──────────────────────────────────────────────────

    async def _browser_join(self) -> None:
        """
        Headless browser automation with:
          • RTCPeerConnection audio hook (add_init_script)
          • Lobby polling up to LOBBY_TIMEOUT seconds
          • WebRTC audio capture (falls back to WASAPI if hook yields nothing)
        """
        await self._launch_browser()
        self._page = await self._context.new_page()

        # Install audio hook + anti-detection BEFORE the first navigation
        await self._page.add_init_script(SPEAKER_HOOK_JS)
        await self._page.add_init_script(WEBRTC_HOOK_JS)
        await self._page.add_init_script(_FINGERPRINT_SPOOF_JS)

        web_url = _build_webclient_url(self.url)
        print(f"[ZoomSDKBot] Browser fallback → {web_url}")
        await self._page.goto(web_url, wait_until="domcontentloaded", timeout=25_000)
        await asyncio.sleep(2)

        await self._mute_av_pre_join()
        await self._enter_name()
        await self._click_join()
        await self._dismiss_audio_dialog()

        self._joined_via = "browser"

        self._participant_task  = asyncio.create_task(self._poll_participants())
        self._panel_task        = asyncio.create_task(self._keep_opening_participants_panel())
        asyncio.create_task(self._delayed_debug_dump())

        print("[ZoomSDKBot] Waiting for meeting admission (browser)…")
        await self._poll_browser_admission()
        print("[ZoomSDKBot] Browser join flow complete ✅")

        # Start standard tracking after confirming admission
        self._start_tracking(self._page, platform='zoom')

    async def _delayed_debug_dump(self) -> None:
        await asyncio.sleep(30)
        try:
            await dump_speakers(self._page, "zoom", "in_meeting")
        except Exception:
            pass



    async def _keep_opening_participants_panel(self) -> None:
        """Keep the participants panel open for the duration of the meeting.

        Checks every 5 s.  Only clicks when the panel is currently CLOSED
        (aria-label starts with "open").  Uses JS .click() instead of
        Playwright click to avoid 'outside of viewport' errors.
        """
        for _ in range(24):  # up to 120 s
            await asyncio.sleep(5)
            try:
                if not self._page or self._page.is_closed():
                    return
                clicked = await self._page.evaluate("""() => {
                    const btn = document.querySelector(
                        'button[aria-label*="participants list" i]'
                    );
                    if (!btn) return 'not_found';
                    const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                    if (label.startsWith('open')) {
                        btn.click();
                        return 'clicked';
                    }
                    return 'already_open';
                }""")
                if clicked == "clicked":
                    print("[ZoomSDKBot] Opened participants panel via JS click.")
                    await asyncio.sleep(1)  # let the panel render
            except asyncio.CancelledError:
                return
            except Exception as exc:
                print(f"[ZoomSDKBot] Panel task error: {exc}")

    async def _open_participants_panel(self) -> None:
        """Open the participants panel via JS click (avoids viewport issues)."""
        try:
            result = await self._page.evaluate("""() => {
                const btn = document.querySelector(
                    'button[aria-label*="participants list" i]'
                );
                if (!btn) return 'not_found';
                const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                if (label.startsWith('open')) {
                    btn.click();
                    return 'clicked';
                }
                return 'already_open';
            }""")
            if result == "clicked":
                print("[ZoomSDKBot] Opened participants panel (stop-time).")
                await asyncio.sleep(1)
            elif result == "already_open":
                print("[ZoomSDKBot] Participants panel already open.")
            else:
                print("[ZoomSDKBot] Participants panel button not found in DOM.")
        except Exception:
            pass

    async def _poll_participants(self) -> None:
        """Background task: scrape participant names from Zoom web client every 3 s."""
        _JS = """() => {
            function isName(s) {
                if (!s || s.length < 2 || s.length > 60) return false;
                if (/^[a-z(]/.test(s)) return false;  // reject lowercase-start and UI labels like (Me), (Host)
                const BAD = /^(unmute|mute|video|stop|start|share|chat|react|more|leave|end|raise|lower|hand|pin|spotlight|rename|remove|host|co-host|waiting|lobby|admit|participants|security|record|closed|caption|whiteboard|apps|zoom|workplace|ai companion|view|you$|the host|mindx)/i;
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

            // Strategy 1: Specific Zoom class selectors (video tiles + participants panel)
            const ZOOM_SELECTORS = [
                '[class*="video-avatar__avatar-name"]',
                '[class*="video-avatar__avatar-title"]',
                '[class*="display-name"]',
                '[class*="participant-name"]',
                '[class*="participants-item__display-name"]',
                '[class*="participants-item"] [class*="name"]',
                '[class*="user-name"]',
                '[class*="video-avatar"] [class*="name"]',
                '[class*="attendees"] [class*="name"]',
                '.video-avatar__avatar-name',
                '.send-video-container__display-name',
                '[class*="roster"] [class*="name"]',
            ];
            for (const sel of ZOOM_SELECTORS) {
                try {
                    for (const el of document.querySelectorAll(sel)) {
                        const t = (el.innerText || el.textContent || '').split('\n')[0].trim();
                        if (isName(t)) found.add(t);
                    }
                } catch(e) {}
            }

            return [...found];
        }"""
        await asyncio.sleep(3)  # initial wait
        while True:
            try:
                if self._page and not self._page.is_closed():
                    names = await self._page.evaluate(_JS)
                    print(f"[ZoomSDKBot] Poll: {names or []}")
                    for n in (names or []):
                        self._seen_participants.add(n)
                    if self._seen_participants:
                        await self._push_names_to_hook(self._page, list(self._seen_participants))
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(3)

    async def _mute_av_pre_join(self) -> None:
        for label in ("Mute", "Stop Video", "Turn off my microphone", "Turn off my video"):
            try:
                btn = await self._page.query_selector(f'button[aria-label*="{label}" i]')
                if btn and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(0.25)
            except Exception:
                pass

    async def _enter_name(self) -> None:
        try:
            inp = await self._page.wait_for_selector('input[type="text"]', timeout=10_000)
            await inp.click()
            await inp.fill(self.bot_name)
            await asyncio.sleep(0.4)
            print("[ZoomSDKBot] Name entered.")
        except Exception as exc:
            print(f"[ZoomSDKBot] Name input not found: {exc}")

    async def _click_join(self) -> None:
        selectors = [
            'button[role="button"]:has-text("Join")',
            'button:has-text("Join")',
            'a:has-text("Join from Your Browser")',
            'a:has-text("Join")',
        ]
        for sel in selectors:
            try:
                btn = await self._page.wait_for_selector(sel, timeout=5_000)
                if btn and await btn.is_visible():
                    await btn.click(force=True)
                    print(f"[ZoomSDKBot] Clicked join ({sel})")
                    return
            except Exception:
                pass

        await self._page.screenshot(path=f"logs/zoom_no_join_{int(time.time())}.png")
        raise RuntimeError("Could not find or click the Zoom join button")

    async def _dismiss_audio_dialog(self) -> None:
        for label in ("Join Audio by Computer", "Computer Audio", "Join Audio"):
            try:
                btn = await self._page.wait_for_selector(
                    f'button:has-text("{label}")', timeout=5_000
                )
                await btn.click()
                print(f"[ZoomSDKBot] Dismissed audio dialog: '{label}'")
                return
            except Exception:
                pass

    async def _poll_browser_admission(self) -> None:
        """
        Alternate between checking for the Leave button (in-meeting) and
        checking page text for lobby indicators.  Waits up to LOBBY_TIMEOUT s.
        """
        in_meeting_sel = (
            'button[aria-label*="Leave" i], '
            'button[aria-label*="End" i], '
            'button:has-text("Leave"), '
            'button:has-text("End"), '
            '.meeting-client, '
            '#wc-container-left, '
            '#wc-footer, '
            '[class*="footer-button__button"], '
            '[class*="meeting-info-container"]'
        )
        lobby_keywords = (
            "please wait",
            "waiting for the host",
            "waiting room",
            "waiting to join",
            "let you in soon",
        )
        deadline = time.time() + self.LOBBY_TIMEOUT

        while time.time() < deadline:
            try:
                el = await self._page.query_selector(in_meeting_sel)
                if el and await el.is_visible():
                    print("[ZoomSDKBot] In meeting (browser).")
                    return
            except Exception:
                pass

            try:
                content = (await self._page.content()).lower()
                if any(kw in content for kw in lobby_keywords):
                    remaining = int(deadline - time.time())
                    print(
                        f"[ZoomSDKBot] In lobby — waiting for host "
                        f"({remaining} s remaining)…"
                    )
            except Exception:
                pass

            await asyncio.sleep(8)

        print(f"[ZoomSDKBot] Could not confirm admission after {self.LOBBY_TIMEOUT}s — proceeding anyway.")

    # ── Watchdog (session resilience) ─────────────────────────────────────────

    async def _watchdog(self) -> None:
        """
        Checks meeting liveness every WATCHDOG_INTERVAL s.
        Attempts one automatic rejoin if the bot is disconnected.
        """
        await asyncio.sleep(self.WATCHDOG_INTERVAL)  # initial grace period

        while not self._stop_requested:
            await asyncio.sleep(self.WATCHDOG_INTERVAL)
            try:
                if await self._is_disconnected():
                    if self._reconnect_count < self.MAX_RECONNECTS:
                        self._reconnect_count += 1
                        print(
                            f"[ZoomSDKBot] Disconnected — rejoining "
                            f"({self._reconnect_count}/{self.MAX_RECONNECTS})…"
                        )
                        await self._rejoin()
                    else:
                        print("[ZoomSDKBot] Max reconnects reached; watchdog stopping.")
                        break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                print(f"[ZoomSDKBot] Watchdog error: {exc}")

    async def _is_disconnected(self) -> bool:
        if self._page is None or self._page.is_closed():
            return True
        try:
            if self._joined_via == "sdk":
                status = await self._page.evaluate(
                    "window._mindx ? window._mindx.status : 'unknown'"
                )
                return status in ("disconnected", "left", "error")
            else:
                el = await self._page.query_selector(
                    'button[aria-label*="Leave" i], button:has-text("Leave"), .meeting-client'
                )
                return el is None
        except Exception:
            return True

    async def _rejoin(self) -> None:
        """Tear down current session and restart the join flow."""
        self._joined_via = "none"

        self.handles_audio = False
        await self._cleanup_browser()

        # if prev_via == "sdk" and self._sdk_configured() and self._meeting_id:
        #     try:
        #         await self._sdk_join()
        #         return
        #     except Exception as exc:
        #         print(f"[ZoomSDKBot] SDK rejoin failed ({exc}). Trying browser…")

        await self._browser_join()

    # ── Public interface ───────────────────────────────────────────────────────

    async def start(self) -> None:
        print(f"[ZoomSDKBot] Starting → {self.url}")
        os.makedirs("logs", exist_ok=True)

        # Optional: pre-authenticate REST API for post-meeting participant list
        # if self._rest_configured():
        #     try:
        #         self._access_token = self._get_access_token()
        #         print("[ZoomSDKBot] REST API authenticated ✅")
        #     except Exception as exc:
        #         print(f"[ZoomSDKBot] REST auth failed (participant API will be skipped): {exc}")

        # ── Join attempt cascade ──
        # if self._sdk_configured() and self._meeting_id:
        #     try:
        #         await self._sdk_join()
        #         self._watchdog_task = asyncio.create_task(self._watchdog(), name="zoom-watchdog")
        #         return
        #     except Exception as exc:
        #         print(f"[ZoomSDKBot] SDK join failed ({exc}). Falling back to browser.")
        #         await self._cleanup_browser()

        await self._browser_join()
        self._watchdog_task = asyncio.create_task(self._watchdog(), name="zoom-watchdog")

    async def stop(self) -> List[str]:
        print("[ZoomSDKBot] Stopping…")
        self._stop_requested = True

        # Final scrape BEFORE cancelling tasks (page still alive)
        if self._page and not self._page.is_closed():
            await self._open_participants_panel()
            await asyncio.sleep(1)
            try:
                names = await self._page.evaluate("""() => {
                    function isName(s) {
                        if (!s || s.length < 2 || s.length > 60) return false;
                        if (/^[a-z(]/.test(s)) return false;
                        const BAD = /^(unmute|mute|video|stop|start|share|chat|react|more|leave|end|raise|lower|hand|pin|spotlight|rename|remove|host|co-host|waiting|lobby|admit|participants|security|record|closed|caption|whiteboard|apps|zoom|workplace|ai companion|view|you$|the host|mindx)/i;
                        if (BAD.test(s)) return false;
                        if (/[0-9]+[.][0-9]+/.test(s)) return false;
                        const words = s.trim().split(/\\s+/);
                        if (words.length > 5) return false;
                        if (words.length >= 4) {
                            const h = Math.floor(words.length / 2);
                            if (words.slice(0, h).join(' ') === words.slice(h).join(' ')) return false;
                        }
                        return true;
                    }
                    const found = new Set();
                    const ZOOM_SELECTORS = [
                        '[class*="video-avatar__avatar-name"]',
                        '[class*="video-avatar__avatar-title"]',
                        '[class*="display-name"]',
                        '[class*="participant-name"]',
                        '[class*="participants-item__display-name"]',
                        '[class*="participants-item"] [class*="name"]',
                        '[class*="user-name"]',
                        '[class*="video-avatar"] [class*="name"]',
                        '[class*="attendees"] [class*="name"]',
                        '.video-avatar__avatar-name',
                        '.send-video-container__display-name',
                        '[class*="roster"] [class*="name"]',
                    ];
                    for (const sel of ZOOM_SELECTORS) {
                        try {
                            for (const el of document.querySelectorAll(sel)) {
                                const t = (el.innerText || el.textContent || '').split('\\n')[0].trim();
                                if (isName(t)) found.add(t);
                            }
                        } catch(e) {}
                    }
                    return [...found];
                }""")
                print(f"[ZoomSDKBot] Final scrape found: {names}")
                for n in (names or []):
                    self._seen_participants.add(n)
            except Exception as e:
                print(f"[ZoomSDKBot] Final scrape error: {e}")

        # Cancel participant polling task
        if self._participant_task and not self._participant_task.done():
            self._participant_task.cancel()
            try:
                await self._participant_task
            except asyncio.CancelledError:
                pass

        # Cancel participants panel task
        if self._panel_task and not self._panel_task.done():
            self._panel_task.cancel()
            try:
                await self._panel_task
            except asyncio.CancelledError:
                pass

        await self._stop_tracking(self._page)

        # Cancel watchdog
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass

        if self._page and not self._page.is_closed():
            # Click Leave via JS to avoid viewport issues (footer may be off-screen during screenshare)
            try:
                await self._page.evaluate("""() => {
                    const btn = document.querySelector(
                        'button[aria-label*="Leave" i], button[aria-label*="End" i]'
                    );
                    if (btn) btn.click();
                }""")
                await asyncio.sleep(1)
            except Exception:
                pass

        self.participants = sorted(self._seen_participants)

        await self._cleanup_browser()
        return self.participants
