"""Spike: does a browser click wake Claude Code?

    python spike/server.py          # http://127.0.0.1:8765

GET  /        a page with one button
POST /fire    the button posts here; the event is queued
GET  /wait    long-polls until an event exists, then returns it as JSON
"""
import asyncio
import json
import time
from http import HTTPStatus

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()
queue: asyncio.Queue = asyncio.Queue()

PAGE = """<!doctype html><title>spike</title>
<body style="font-family:sans-serif;padding:2rem">
<h2>Claude wake-up spike</h2>
<button id=b style="font-size:1.5rem;padding:1rem">I submitted</button>
<pre id=out></pre>
<script>
b.onclick = async () => {
  const r = await fetch('/fire', {method:'POST', headers:{'content-type':'application/json'},
     body: JSON.stringify({kind:'submission', problem:424, verdict:'wrong answer', at:new Date().toISOString()})});
  out.textContent += 'fired ' + r.status + '\\n';
};
</script></body>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE


@app.post("/fire")
async def fire(body: dict):
    await queue.put(body)
    return {"queued": True}


@app.get("/wait")
async def wait():
    # Hold the request open until an event arrives (max 25 min).
    try:
        ev = await asyncio.wait_for(queue.get(), timeout=25 * 60)
    except asyncio.TimeoutError:
        return JSONResponse({"kind": "timeout"}, status_code=HTTPStatus.NO_CONTENT)
    return ev


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
