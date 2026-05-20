import sys
import asyncio
import uvicorn

# This explicitly sets the ProactorEventLoop BEFORE Uvicorn does any internal magic
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

if __name__ == "__main__":
    print("Starting Mindx Server with fixed Asyncio Windows policy...")
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000)
