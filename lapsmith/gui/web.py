"""Optional LAN view (FastAPI): serve the same status on a phone / second screen.

Lazy-imports fastapi + uvicorn. Runs in a background thread so it doesn't block
the Qt loop. Read-only - it mirrors the overlay, it does not control the loop.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from .. import PRODUCT_NAME

_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<title>__PRODUCT__</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{background:#0e1014;color:#eaeaea;font-family:system-ui,monospace;margin:0;padding:16px}
.card{background:#161922;border:1px solid #2a3550;border-radius:12px;padding:16px;max-width:640px;margin:auto}
.k{color:#8cf}.chg{color:#ffd479;font-weight:700}.msg{color:#999;font-size:13px}</style></head>
<body><div class=card id=app>connecting...</div>
<script>
async function tick(){try{const r=await fetch('/status');const s=await r.json();
let h=`<h2>__PRODUCT__ <span class=k>${s.phase}</span></h2>`;
if(s.car)h+=`<div>${s.car}</div>`;
if(s.live){const speed=s.live.speed_text||`${s.live.speed_mph} mph`;h+=`<div>Speed ${speed} | RPM ${s.live.rpm} | Gear ${s.live.gear} | ${s.live.lat_g}g | ${s.live.drivetrain}</div>`;}
if(s.iteration)h+=`<div>iteration ${s.iteration} | ${s.discipline||''} ${s.best_segment_s?('| best '+s.best_segment_s.toFixed(2)+'s'):''}</div>`;
if(s.change){const f=Object.entries(s.change.fields).map(([k,v])=>k+'='+v).join(', ');
h+=`<p class=chg>NEXT [${s.change.group}]: ${f}</p><div>${s.change.detail}</div><div class=k>feel: ${s.change.feel}</div>`;}
if(s.messages)h+=s.messages.map(m=>`<div class=msg>${m}</div>`).join('');
document.getElementById('app').innerHTML=h;}catch(e){}}
setInterval(tick,300);tick();
</script></body></html>"""


def serve(status_fn: Callable[[], dict], host: str = "0.0.0.0", port: int = 8077
          ) -> Optional[threading.Thread]:
    """Start the LAN view in a daemon thread. Returns the thread, or None if
    fastapi/uvicorn aren't installed."""
    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, JSONResponse
        import uvicorn
    except Exception:
        return None

    app = FastAPI()

    page = _PAGE.replace("__PRODUCT__", PRODUCT_NAME)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return page

    @app.get("/status", response_class=JSONResponse)
    def status():
        return status_fn()

    def run():
        uvicorn.run(app, host=host, port=port, log_level="warning")

    th = threading.Thread(target=run, name="lapsmith-web", daemon=True)
    th.start()
    return th
