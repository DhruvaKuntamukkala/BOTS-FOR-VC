"""
WebRTC Audio Capture
────────────────────
Captures meeting audio by polling Float32 PCM chunks from
``window._mindx.audioChunks`` — a global buffer populated by the
RTCPeerConnection track hook that is injected into every page before it
loads (via ``add_init_script`` or the standalone zoom_page.html).

Usage
-----
    capture = WebRTCCapture(page, "logs/zoom_1.wav")
    await capture.start()                   # starts background polling
    ...                                     # meeting runs
    wav_path = await capture.stop()         # flushes + writes WAV

The polling interval is 2 s by default.  On ``stop()`` a final drain is
performed to flush any chunks that arrived between the last poll and now.

Output
------
16-bit PCM, mono, 16 kHz WAV — matching the rest of the pipeline.
If no audio was captured a 1-second silent WAV is written so downstream
metadata generation never sees a missing file.
"""

from __future__ import annotations

import asyncio
import os
import wave
from typing import List, Optional

import numpy as np


_SAMPLE_RATE = 16_000
_CHANNELS    = 1
_SAMPWIDTH   = 2  # int16


# JavaScript drain expression — safe even if _mindx was never initialised
_DRAIN_JS = (
    "window._mindx && typeof window._mindx.drainAudio === 'function'"
    " ? window._mindx.drainAudio() : []"
)

# Shared WebRTC audio hook — inject via add_init_script before page.goto().
# Patches RTCPeerConnection so every incoming audio track is piped through an
# AudioContext ScriptProcessor into window._mindx.audioChunks (Float32 arrays).
# A silent keepalive oscillator keeps the AudioContext in "running" state even
# inside headless Chrome (where it would otherwise auto-suspend).
WEBRTC_HOOK_JS = """
(function () {
  if (window._mindxHookInstalled) return;
  window._mindxHookInstalled = true;

  window._mindx = window._mindx || { audioChunks: [], _processors: [], _audioCtx: null, _capturedStreams: new Set() };

  window._mindx.drainAudio = function () {
    return window._mindx.audioChunks.splice(0);
  };

  function getCtx() {
    if (!window._mindx._audioCtx) {
      window._mindx._audioCtx = new AudioContext({ sampleRate: 16000 });
      try {
        var osc  = window._mindx._audioCtx.createOscillator();
        var gain = window._mindx._audioCtx.createGain();
        gain.gain.value = 0;
        osc.connect(gain);
        gain.connect(window._mindx._audioCtx.destination);
        osc.start();
      } catch(e) {}
    }
    var ctx = window._mindx._audioCtx;
    if (ctx.state !== 'running') ctx.resume().catch(function(){});
    return ctx;
  }

  window._mindx._captureStream = function (stream) {
    if (!stream || !stream.getAudioTracks) return;
    if (stream.getAudioTracks().length === 0) return;
    var id = stream.id || Math.random().toString();
    if (window._mindx._capturedStreams.has(id)) return;
    window._mindx._capturedStreams.add(id);
    try {
      var ctx    = getCtx();
      var source = ctx.createMediaStreamSource(stream);
      var proc   = ctx.createScriptProcessor(4096, 1, 1);
      proc.onaudioprocess = function (e) {
        var raw  = e.inputBuffer.getChannelData(0);
        var copy = new Float32Array(raw.length);
        copy.set(raw);
        window._mindx.audioChunks.push(Array.from(copy));
        if (window._mindx.audioChunks.length > 1200)
          window._mindx.audioChunks.splice(0, window._mindx.audioChunks.length - 1200);
      };
      source.connect(proc);
      proc.connect(ctx.destination);
      window._mindx._processors.push({ source: source, processor: proc });
      console.log('[MindxHook] Capturing stream id=' + id + ' tracks=' + stream.getAudioTracks().length);
    } catch(err) {
      console.error('[MindxHook] captureStream error:', err);
    }
  };

  window._mindx._captureTrack = function (track, stream) {
    var ms = (stream && stream.getAudioTracks && stream.getAudioTracks().length > 0)
               ? stream : new MediaStream([track]);
    window._mindx._captureStream(ms);
  };

  // Hook 1: RTCPeerConnection incoming tracks
  var _OrigRTC = window.RTCPeerConnection;
  function MindxRTC() {
    var pc = new (Function.prototype.bind.apply(
      _OrigRTC, [null].concat(Array.prototype.slice.call(arguments))
    ))();
    pc.addEventListener('track', function (ev) {
      if (ev.track.kind === 'audio')
        window._mindx._captureTrack(ev.track, ev.streams && ev.streams[0]);
    });
    return pc;
  }
  MindxRTC.prototype = _OrigRTC.prototype;
  Object.setPrototypeOf(MindxRTC, _OrigRTC);
  window.RTCPeerConnection = MindxRTC;

  // Hook 2: <audio> / <video> elements (Zoom routes audio here too)
  var _origPlay = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    if (this.srcObject) window._mindx._captureStream(this.srcObject);
    return _origPlay.apply(this, arguments);
  };
  Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {
    get: function () { return this._mindxSrcObject; },
    set: function (val) {
      this._mindxSrcObject = val;
      if (val) window._mindx._captureStream(val);
    },
    configurable: true
  });
})();
"""


class WebRTCCapture:
    """
    Polls ``window._mindx.drainAudio()`` on a Playwright *page* and
    accumulates the Float32 PCM frames returned.  Saves a WAV on stop.

    Parameters
    ----------
    page:
        An *async* Playwright ``Page`` that already has the _mindx hook
        installed (either via ``add_init_script`` or by navigating to the
        Zoom SDK HTML page).
    output_file:
        Destination WAV path (parent directory is created if missing).
    poll_interval:
        Seconds between drain calls while the meeting is in progress.
    """

    def __init__(
        self,
        page,
        output_file: str,
        poll_interval: float = 2.0,
    ) -> None:
        self._page          = page
        self._output_file   = output_file
        self._poll_interval = poll_interval
        self._frames: List[np.ndarray] = []
        self._task: Optional[asyncio.Task] = None
        self._running       = False

    # ── Public API ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin background polling."""
        self._running = True
        self._frames  = []
        self._task    = asyncio.create_task(self._poll_loop(), name="webrtc-capture-poll")
        print(f"[WebRTCCapture] Polling started → {self._output_file}")

    async def stop(self) -> str:
        """
        Stop polling, perform a final drain, write WAV.

        Returns the absolute path to the written WAV file.
        """
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # Final drain: capture everything that arrived after the last poll
        await self._drain_once()
        path = self._save_wav()
        print(f"[WebRTCCapture] Stopped — {len(self._frames)} chunks → {path}")
        return path

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._poll_interval)
            await self._drain_once()

    async def _drain_once(self) -> None:
        """Call drainAudio() on the page and append returned frames."""
        if self._page is None or self._page.is_closed():
            return
        try:
            chunks = await self._page.evaluate(_DRAIN_JS)
            for chunk in chunks:
                arr = np.array(chunk, dtype=np.float32)
                if arr.size > 0:
                    self._frames.append(arr)
        except Exception as exc:
            # Page navigating away / context destroyed — stop gracefully
            print(f"[WebRTCCapture] Drain skipped: {exc}")
            self._running = False

    def _save_wav(self) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(self._output_file)), exist_ok=True)

        if self._frames:
            audio = np.concatenate(self._frames)
            audio = np.clip(audio, -1.0, 1.0)
            pcm   = (audio * 32_767).astype(np.int16)
        else:
            # 1 second of silence so downstream never sees a zero-byte file
            pcm = np.zeros(_SAMPLE_RATE, dtype=np.int16)

        with wave.open(self._output_file, "wb") as wf:
            wf.setnchannels(_CHANNELS)
            wf.setsampwidth(_SAMPWIDTH)
            wf.setframerate(_SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())

        return os.path.abspath(self._output_file)
