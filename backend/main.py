import sys
import asyncio

# Fix for Playwright subprocess error on Windows when using Uvicorn
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import uuid

from backend.orchestrator.manager import Orchestrator

app = FastAPI(title="Mindx Orchestrator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = Orchestrator()

class StartMeetingRequest(BaseModel):
    meeting_url: str

class StopMeetingRequest(BaseModel):
    session_id: str

@app.post("/start-meeting")
async def start_meeting(req: StartMeetingRequest):
    session_id = str(uuid.uuid4())
    try:
        await orchestrator.start_session(session_id, req.meeting_url)
        return {"status": "success", "session_id": session_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=repr(e))

@app.post("/stop-meeting")
async def stop_meeting(req: StopMeetingRequest):
    try:
        result = await orchestrator.stop_session(req.session_id)
        return {"status": "success", "result": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/status")
async def get_status(session_id: str):
    status = orchestrator.get_status(session_id)
    if not status:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": status}

@app.get("/result")
async def get_result(session_id: str):
    result = orchestrator.get_result(session_id)
    if not result:
        raise HTTPException(status_code=404, detail="Result not found or session still running")
    return result

# @app.get("/zoom-sdk")
# async def zoom_sdk_page():
#     """
#     Serve the Zoom Meeting SDK HTML page used by ZoomSDKBot.
#     The bot navigates to http://localhost:8000/zoom-sdk in a headless browser,
#     then calls window._mindx.join({...}) to enter the meeting.
#     """
#     html_path = os.path.join(
#         os.path.dirname(__file__), "zoom_sdk", "zoom_page.html"
#     )
#     if not os.path.exists(html_path):
#         raise HTTPException(status_code=404, detail="zoom_page.html not found")
#     return FileResponse(html_path, media_type="text/html")


# @app.post("/teams/callback")
# async def teams_callback():
#     """Callback endpoint for Microsoft Graph Calling API events."""
#     return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
