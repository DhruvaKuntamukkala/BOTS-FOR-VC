import asyncio
import json
import time
from abc import ABC, abstractmethod
from typing import List, Optional

class BaseBot(ABC):
    def __init__(self, url: str, use_rtms: bool = False) -> None:
        self.url        = url
        self.use_rtms   = use_rtms
        self.bot_name   = "Mindx Bot"
        self.participants: List[str] = []

        # Set to True by bots that manage their own audio capture (WebRTC).
        self.handles_audio: bool      = False
        self.audio_file:    Optional[str] = None

        # Speaker tracking
        self._speaker_logs: List[dict] = []   # [{name, start, end}] in seconds from meeting start
        self._tracking_task: Optional[asyncio.Task] = None
        self._meeting_start: float = 0.0
        self.audio_start_time: float = 0.0

    # ── Speaker tracking ──────────────────────────────────────────────────────

    async def _track_speakers(self, page) -> None:
        """
        Drains window._mindxSpeaker.events every 2 seconds.
        The hook (SPEAKER_HOOK_JS) is injected before page load via
        add_init_script(), so it runs before any meeting JS executes.
        """
        try:
            while True:
                await asyncio.sleep(2.0)
                try:
                    events = await page.evaluate("window._mindxSpeaker ? window._mindxSpeaker.drain() : []")
                    for ev in (events or []):
                        name = (ev.get("name") or "").strip()
                        if name and name != self.bot_name:
                            self._speaker_logs.append({
                                "name":  name,
                                "start": round(float(ev.get("start", 0)), 2),
                                "end":   round(float(ev.get("end",   0)), 2),
                            })
                            print(f"[SpeakerTrack] {name}: {ev.get('start'):.1f}s → {ev.get('end'):.1f}s")
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    def _start_tracking(self, page, platform: str = 'generic') -> None:
        """
        Start draining the in-page speaker hook.
        Call this after the bot has confirmed it is inside the meeting.
        """
        self._meeting_start = time.time()

        # Calculate how many seconds of audio have already been recorded
        elapsed_audio = time.time() - self.audio_start_time if self.audio_start_time > 0 else 0.0

        # Enable tracking and offset timestamps to align precisely with the audio file
        asyncio.create_task(self._enable_js_tracking(page, elapsed_audio, platform))

        self._speaker_logs  = []
        self._tracking_task = asyncio.create_task(self._track_speakers(page))

    async def _enable_js_tracking(self, page, elapsed: float, platform: str = 'generic') -> None:
        try:
            await page.evaluate(f"if(window._mindxSpeaker) window._mindxSpeaker.startTracking({elapsed})")
            await page.evaluate(f"if(window._mindxSpeaker) window._mindxSpeaker.setPlatform('{platform}')")
            print(f"[SpeakerTrack] Tracking started. Audio offset: {elapsed:.1f}s, Platform: {platform}")
        except Exception as e:
            print(f"[SpeakerTrack] Could not start JS tracking: {e}")

    async def _stop_tracking(self, page=None) -> None:
        """Cancel the drain task and flush any open interval from the hook."""
        if self._tracking_task and not self._tracking_task.done():
            self._tracking_task.cancel()
            try:
                await self._tracking_task
            except asyncio.CancelledError:
                pass

        # Flush the currently-open interval from the JS side
        if page:
            try:
                events = await page.evaluate(
                    "window._mindxSpeaker ? window._mindxSpeaker.flush() : []"
                )
                for ev in (events or []):
                    name = (ev.get("name") or "").strip()
                    if name and name != self.bot_name:
                        self._speaker_logs.append({
                            "name":  name,
                            "start": round(float(ev.get("start", 0)), 2),
                            "end":   round(float(ev.get("end",   0)), 2),
                        })
            except Exception:
                pass

    async def _push_names_to_hook(self, page, names: List[str]) -> None:
        """
        Tell the in-page hook about known participant names so it can use
        the name-match signal in addition to aria-label scanning.
        """
        try:
            await page.evaluate(
                f"if(window._mindxSpeaker) window._mindxSpeaker.setNames({json.dumps(names)})"
            )
        except Exception:
            pass

    def get_speaker_logs(self) -> List[dict]:
        return list(self._speaker_logs)

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    async def start(self) -> None:
        """Launch browser / API client and join the meeting."""

    @abstractmethod
    async def stop(self) -> List[str]:
        """Leave the meeting and return the list of participant display names."""
