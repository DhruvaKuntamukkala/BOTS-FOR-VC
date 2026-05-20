import asyncio
import time
import traceback
from typing import Dict, Any

from backend.strategies.selector import detect_platform, StrategySelector
from backend.bots.base_bot import BaseBot
from backend.audio.capture import AudioCapture
from backend.metadata.generator import MetadataGenerator


class Session:
    def __init__(self, session_id: str, url: str):
        self.session_id = session_id
        self.url = url
        self.status = "Waiting"
        self.bot: BaseBot = None
        self.audio: AudioCapture = None
        self.result: Dict[str, Any] = {}
        self.start_time: float = 0
        self.end_time: float = 0
        self.participants = []
        self._launch_task: asyncio.Task = None


class Orchestrator:
    def __init__(self):
        self.sessions: Dict[str, Session] = {}

    async def _launch_bot(self, session: Session):
        try:
            await session.bot.start()
            # Only update status if stop hasn't been called yet
            if session.status == "Bot joining meeting...":
                session.status = "Recording..."
        except Exception as e:
            session.status = f"Failed: {repr(e)}"
            print(f"Bot session failed: {repr(e)}")
            traceback.print_exc()

    async def start_session(self, session_id: str, url: str):
        session = Session(session_id, url)
        self.sessions[session_id] = session

        platform = detect_platform(url)
        if platform.value == "unknown":
            session.status = "Failed: Unknown platform"
            raise ValueError("Unknown platform from URL")

        bot_class, use_rtms = StrategySelector.get_strategy(platform)
        session.bot = bot_class(url, use_rtms)
        session.audio = AudioCapture(session_id, url)

        # Start time and WASAPI recording begin immediately — not after bot joins.
        # This guarantees audio is captured and duration is always positive,
        # even if the bot takes a long time to join or the user stops early.
        session.start_time = time.time()
        session.status = "Bot joining meeting..."
        session.audio.start_recording()
        session.bot.audio_start_time = session.start_time

        session._launch_task = asyncio.create_task(self._launch_bot(session))

    async def stop_session(self, session_id: str) -> Dict[str, Any]:
        if session_id not in self.sessions:
            raise ValueError("Session not found")

        session = self.sessions[session_id]
        if session.status == "Completed":
            return getattr(session, 'result', {})
        if session.status.startswith("Failed"):
            raise ValueError(f"Session failed: {session.status}")

        session.status = "Processing..."
        session.end_time = time.time()

        # Cancel the launch task if bot is still joining
        if session._launch_task and not session._launch_task.done():
            session._launch_task.cancel()
            try:
                await session._launch_task
            except (asyncio.CancelledError, Exception):
                pass

        try:
            # Stop WASAPI — always running since start_session
            wasapi_file = session.audio.stop_recording()

            # Stop bot and get participants + DOM speaker timeline
            try:
                session.participants = await asyncio.wait_for(
                    session.bot.stop(), timeout=60
                )
            except Exception as e:
                print(f"[Orchestrator] Bot stop error: {e!r}")
                import traceback
                traceback.print_exc()
                session.participants = []

            if getattr(session.bot, "handles_audio", False) and getattr(session.bot, "audio_file", None):
                try:
                    os.remove(wasapi_file)  # Delete silent WASAPI fallback
                except Exception:
                    pass
                audio_file = session.bot.audio_file
                print(f"[Orchestrator] Using bot's WebRTC audio: {audio_file}")
            else:
                audio_file = wasapi_file

            dom_timeline = session.bot.get_speaker_logs()
            print(f"[Orchestrator] DOM speaker timeline: {len(dom_timeline)} intervals.")

            duration = int(session.end_time - session.start_time)

            # ── RunPod NeMo diarization (optional) ───────────────────────────
            speaker_timeline = _build_speaker_timeline(
                audio_file, dom_timeline, session.participants
            )

            meta_gen = MetadataGenerator()
            metadata_file = meta_gen.generate(
                session.audio.base_name,
                session.start_time,
                session.end_time,
                duration,
                session.participants,
                speaker_timeline=speaker_timeline,
            )

            session.status = "Completed"
            session.result = {
                "audio_file":      audio_file,
                "metadata_file":   metadata_file,
                "participants":    session.participants,
                "speaker_timeline": speaker_timeline,
                "duration":        duration,
                "start_time":      session.start_time,
                "end_time":        session.end_time,
            }
            return session.result

        except Exception as e:
            session.status = f"Failed: {str(e)}"
            raise e

    def get_status(self, session_id: str) -> str:
        if session_id in self.sessions:
            return self.sessions[session_id].status
        return None

    def get_result(self, session_id: str) -> Dict[str, Any]:
        session = self.sessions.get(session_id)
        if session and session.status == "Completed":
            return session.result
        return None


def _build_speaker_timeline(
    audio_file: str,
    dom_timeline: list,
    participants: list,
) -> list:
    """
    Try RunPod NeMo diarization + name mapping.
    Falls back to DOM-only timeline if RunPod is not configured or fails.
    """
    from backend.speech.diarize_client import diarize
    from backend.speech.name_mapper import assign_names

    try:
        import config
        runpod_key = getattr(config, "RUNPOD_API_KEY", "") or ""
        runpod_ep  = getattr(config, "RUNPOD_ENDPOINT_ID", "") or ""
    except ImportError:
        import os
        runpod_key = os.environ.get("RUNPOD_API_KEY", "")
        runpod_ep  = os.environ.get("RUNPOD_ENDPOINT_ID", "")

    if runpod_key and runpod_ep and audio_file:
        print("[Orchestrator] Sending audio to RunPod for diarization...")
        nemo_timeline = diarize(audio_file, num_speakers=len(participants) or None)
        if nemo_timeline:
            print(f"[Orchestrator] NeMo returned {len(nemo_timeline)} segments — mapping names.")
            return assign_names(dom_timeline, nemo_timeline)
        print("[Orchestrator] RunPod returned no segments — falling back to DOM timeline.")

    # Fallback: use DOM active-speaker timeline directly
    if dom_timeline:
        def _fmt_time(s: float) -> str:
            m, sc = int(s // 60), int(s % 60)
            return f"{m}:{sc:02d}"

        return [
            {
                "speaker": seg["name"], 
                "start": round(seg["start"], 2), 
                "end": round(seg["end"], 2),
                "duration_sec": round(seg["end"] - seg["start"], 2),
                "duration_formatted": _fmt_time(seg["end"] - seg["start"]),
                "time_range": f"{_fmt_time(seg['start'])}-{_fmt_time(seg['end'])}"
            }
            for seg in dom_timeline
        ]
    return []
