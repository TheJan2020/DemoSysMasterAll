"""Tiny `/demo` landing — six cards, one per vertical."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def demo_landing() -> HTMLResponse:
    return HTMLResponse(_HTML)


_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>PW Demo · Verticals</title>
  <style>
    :root {
      --bg: #0f1216; --panel: #161a21; --panel-2: #1c222b; --border: #242b36;
      --text: #e6e9ef; --dim: #98a2b3; --accent: #6aa3ff;
      --brand: linear-gradient(135deg, #5B2A9E, #3E7BE8 55%, #2BC8E5);
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text);
           font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    .wrap { max-width: 1000px; margin: 0 auto; padding: 56px 24px; }
    h1 { font-size: 28px; margin: 0 0 6px;
         background: var(--brand); -webkit-background-clip: text; background-clip: text;
         color: transparent; }
    p.sub { color: var(--dim); margin: 0 0 36px; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
    .card { background: var(--panel-2); border: 1px solid var(--border);
            border-radius: 12px; padding: 22px; transition: border-color .15s, transform .15s;
            display: flex; flex-direction: column; gap: 8px; min-height: 140px; }
    .card.live { cursor: pointer; }
    .card.live:hover { border-color: var(--accent); transform: translateY(-2px); }
    .card a { color: var(--accent); text-decoration: none; margin-top: auto; font-size: 14px; }
    .card.soon { opacity: 0.45; }
    .card .badge { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em;
                   color: var(--dim); }
    .card h2 { margin: 0; font-size: 16px; }
    .card p { margin: 0; color: var(--dim); font-size: 13px; }
    .topbar { display: flex; justify-content: space-between; align-items: center;
              margin-bottom: 24px; gap: 16px; flex-wrap: wrap; }
    .topbar .home { display: inline-flex; align-items: center; gap: 6px;
                    padding: 8px 14px; border: 1px solid var(--border);
                    border-radius: 8px; color: var(--text); text-decoration: none;
                    background: var(--panel-2); font-size: 13px;
                    transition: border-color .15s, background .15s; }
    .topbar .home:hover { border-color: var(--accent); background: var(--panel); }
    .topbar .home svg { width: 14px; height: 14px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <h1>PW Demo · Vertical apps</h1>
        <p class="sub" style="margin:0;">Six industry demos, each backed by Lena as a Live Representative.</p>
      </div>
      <!-- Full page load to "/" — the master PW Demo app (hash-routed SPA). -->
      <a href="/" class="home" title="Back to PW Demo Master">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <path d="M19 12H5"/><polyline points="12 19 5 12 12 5"/>
        </svg>
        Back to main app
      </a>
    </div>

    <div class="cards">
      <div class="card live">
        <span class="badge">Live</span>
        <h2>Clinic</h2>
        <p>Front-desk receptionist, appointment intake, follow-ups.</p>
        <a href="/demo/clinic/">Open Clinic →</a>
      </div>
      <div class="card live">
        <span class="badge">Live</span>
        <h2>Restaurant</h2>
        <p>Reservations host, party-size planning, dietary notes.</p>
        <a href="/demo/restaurant/">Open Restaurant →</a>
      </div>
      <div class="card soon">
        <span class="badge">Coming soon</span>
        <h2>Gym</h2>
        <p>Membership desk, trial-pass requests, class scheduling.</p>
      </div>
      <div class="card soon">
        <span class="badge">Coming soon</span>
        <h2>School</h2>
        <p>Parent inquiries, attendance, fee status.</p>
      </div>
      <div class="card soon">
        <span class="badge">Coming soon</span>
        <h2>Property Developer</h2>
        <p>Lead intake, unit interest, scheduled viewings.</p>
      </div>
      <div class="card soon">
        <span class="badge">Coming soon</span>
        <h2>Gas Station</h2>
        <p>Loyalty signups, fleet accounts, complaint logging.</p>
      </div>
    </div>
  </div>
</body>
</html>
"""
