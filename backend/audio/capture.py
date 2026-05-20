import os
import sys
import wave
import glob
import sounddevice as sd
import numpy as np
from urllib.parse import urlparse


def _extract_vcname(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        if 'meet.google.com' in netloc:
            return 'meet'
        if 'zoom.us' in netloc:
            return 'zoom'
        if 'webex.com' in netloc:
            return 'webex'
        if 'teams.microsoft.com' in netloc or 'teams.live.com' in netloc:
            return 'teams'
        if 'zoho.com' in netloc or 'zohomeeting.com' in netloc:
            return 'zoho'
        return 'meeting'
    except Exception:
        return 'meeting'


def _find_loopback_device():
    """
    On Windows: find a WASAPI loopback device so we capture system audio
    (what's actually playing — meeting audio) instead of the microphone.
    Falls back to the default input if no loopback device is found.
    Returns (device_index_or_None, wasapi_settings_or_None, channels).
    """
    if sys.platform != "win32":
        return None, None, 1  # non-Windows: just use default mic

    try:
        # Find the WASAPI host API index
        wasapi_hostapi = None
        for i, api in enumerate(sd.query_hostapis()):
            if 'WASAPI' in api['name']:
                wasapi_hostapi = i
                break

        if wasapi_hostapi is None:
            print("[Audio] WASAPI not found, using default input.")
            return None, None, 1

        # Find the default output device (speakers/headphones)
        default_output_idx = sd.default.device[1]
        if default_output_idx < 0:
            # fallback: first WASAPI output device
            for i, dev in enumerate(sd.query_devices()):
                if dev['hostapi'] == wasapi_hostapi and dev['max_output_channels'] > 0:
                    default_output_idx = i
                    break

        if default_output_idx < 0:
            return None, None, 1

        dev_info = sd.query_devices(default_output_idx)
        channels = min(dev_info['max_output_channels'], 2)  # stereo max
        print(f"[Audio] Using WASAPI loopback on: {dev_info['name']} (device {default_output_idx})")
        wasapi_settings = sd.WasapiSettings(loopback=True)
        return default_output_idx, wasapi_settings, channels

    except Exception as e:
        print(f"[Audio] Loopback device lookup failed: {e}. Using default mic.")
        return None, None, 1


class AudioCapture:
    def __init__(self, session_id: str, url: str = '', sample_rate: int = 16000):
        self.session_id = session_id
        self.sample_rate = sample_rate
        self.frames = []
        self.is_recording = False
        self.stream = None
        os.makedirs("logs", exist_ok=True)

        vcname = _extract_vcname(url) if url else 'meeting'
        existing = glob.glob(f"logs/{vcname}_*.wav")
        next_num = len(existing) + 1
        self.base_name = f"{vcname}_{next_num}"
        self.output_file = f"logs/{self.base_name}.wav"

        self._device, self._wasapi, self._channels = _find_loopback_device()

    def _audio_callback(self, indata, _frames, _time_info, _status):
        if self.is_recording:
            self.frames.append(indata.copy())

    def start_recording(self):
        self.is_recording = True
        self.frames = []
        print(f"[Audio] Recording started → {self.output_file}")
        try:
            kwargs = dict(
                samplerate=self.sample_rate,
                channels=self._channels,
                callback=self._audio_callback,
            )
            if self._device is not None:
                kwargs['device'] = self._device
            if self._wasapi is not None:
                kwargs['extra_settings'] = self._wasapi

            self.stream = sd.InputStream(**kwargs)
            self.stream.start()
        except Exception as e:
            print(f"[Audio] Stream failed: {e}. Recording will be silent.")

    def stop_recording(self) -> str:
        self.is_recording = False
        if self.stream:
            self.stream.stop()
            self.stream.close()
        print(f"[Audio] Recording stopped. Frames captured: {len(self.frames)}")

        if self.frames:
            audio_data = np.concatenate(self.frames, axis=0)
            # Mix stereo down to mono if needed
            if audio_data.ndim > 1 and audio_data.shape[1] > 1:
                audio_data = audio_data.mean(axis=1)
            audio_data = np.clip(audio_data, -1.0, 1.0)
            audio_data = (audio_data * 32767).astype(np.int16)
            with wave.open(self.output_file, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(audio_data.tobytes())
        else:
            # Fallback: 1s silent WAV
            with wave.open(self.output_file, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(b'\x00\x00' * self.sample_rate)

        return os.path.abspath(self.output_file)
