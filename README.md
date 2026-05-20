# BOTS-FOR-VC

Automated meeting bots for Zoom and Webex. Joins meetings via browser automation, captures audio, tracks speakers, and generates session metadata.

---

## Features

- Joins Zoom and Webex meetings automatically using Playwright (real Chrome)
- Captures audio via WebRTC hook injected into the browser
- Tracks active speakers from the DOM in real time
- Generates a JSON metadata file with participants, duration, and speaker timeline
- Optional NeMo diarization via RunPod for more accurate speaker separation
- React frontend to launch/stop bots and view results

---

## Project Structure

```
├── run.py
├── config.example.py
├── login_bot.py
├── requirements.txt
└── backend/
    ├── main.py
    ├── orchestrator/
    │   └── manager.py
    ├── strategies/
    │   └── selector.py
    ├── bots/
    │   ├── base_bot.py
    │   ├── zoom_sdk_bot.py
    │   ├── webex_sdk_bot.py
    │   ├── speaker_hook.py
    │   └── speaker_debug.py
    ├── audio/
    │   ├── capture.py
    │   └── webrtc_capture.py
    └── metadata/
        └── generator.py
```

---

## Setup

**1. Install Python dependencies**

```bash
pip install -r requirements.txt
playwright install chromium
```

**2. Configure credentials**

```bash
cp config.example.py config.py
```

Fill in your Zoom / Webex / RunPod credentials in `config.py`.

**3. Install frontend dependencies**

```bash
cd frontend
npm install
```

---

## Running

**Backend** (from repo root):

```bash
python run.py
```

Server starts at `http://localhost:8000`

**Frontend** (in a separate terminal):

```bash
cd frontend
npm run dev
```

UI available at `http://localhost:5173`

---

## API

| Method | Endpoint | Body | Description |
|--------|----------|------|-------------|
| POST | `/start-meeting` | `{ "meeting_url": "..." }` | Launch bot into meeting |
| POST | `/stop-meeting` | `{ "session_id": "..." }` | Stop bot and process results |
| GET | `/status` | `?session_id=...` | Get current session status |
| GET | `/result` | `?session_id=...` | Get final output |

---

## How It Works

1. Submit a meeting URL via the UI or API
2. The backend detects the platform (Zoom / Webex) from the URL
3. A Playwright browser (real Chrome) joins the meeting as a guest
4. A JavaScript hook intercepts the WebRTC audio stream and tracks who is speaking
5. When you stop the session, the bot leaves, audio is saved, and a metadata JSON is written to `logs/`
6. If RunPod is configured, audio is sent to a NeMo diarization endpoint for speaker labeling

---

## Bot Detection Workaround

Zoom and Webex detect standard Playwright browsers as bots. This project uses:

- `channel="chrome"` — launches your real installed Chrome instead of Playwright's bundled Chromium
- `ignore_default_args=["--enable-automation"]` — removes the automation flag
- `--disable-blink-features=AutomationControlled` — hides the `navigator.webdriver` property

---

## Output

Each session produces two files in `logs/`:

- `zoom_1.wav` / `webex_1.wav` — raw audio
- `session_<name>_<timestamp>.json` — metadata with participants, duration, and speaker timeline

---

## Configuration

| Key | Description |
|-----|-------------|
| `ZOOM_ACCOUNT_ID / CLIENT_ID / CLIENT_SECRET` | Zoom Server-to-Server OAuth app |
| `ZOOM_SDK_KEY / SDK_SECRET` | Zoom Meeting SDK app (optional) |
| `WEBEX_ACCESS_TOKEN` | Webex bot token |
| `RUNPOD_API_KEY / ENDPOINT_ID` | RunPod NeMo diarization (optional) |

---

## Requirements

- Python 3.10+
- Google Chrome installed (for bot detection bypass)
- Node.js 18+ (for frontend)
