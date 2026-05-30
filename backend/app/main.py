"""PW Demo Master — FastAPI app."""
from __future__ import annotations

import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

# Make sure our `logger.info(...)` calls across the app actually reach
# the terminal when running under uvicorn. Without this, uvicorn only
# configures its own `uvicorn.*` loggers, so every named logger we
# create with `logging.getLogger("restaurant_live_agent")` etc. is
# silently dropped at INFO and below — which is exactly why
# `ClinicLiveAgent: new call …`, `ElevenLabs TTS connecting …`, and
# the rest never appeared in stdout. Setting basicConfig here installs
# a stderr StreamHandler on the root logger at INFO, which our named
# loggers inherit from.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

from .routers import (
    ai_camera, ai_camera_rules, frigate, homeassistant, live_agent, mqtt,
    sip, sip_live_agent, sip_live_rep,
)
from .services import ai_camera_engine
from .services.mqtt import mqtt_service
from .services.sip_live_agent import sip_live_agent_service
from .services.sip_live_rep import sip_live_rep_service

# Per-vertical demo apps (see DEMOSITEMAP.md).
from .demos import landing as demos_landing
from .demos.clinic import router as demos_clinic
from .demos.clinic.live_agent import clinic_live_agent_service
from .demos.restaurant import router as demos_restaurant
from .demos.restaurant.live_agent import restaurant_live_agent_service
from .demos.restaurant import kitchen_ai_history_task as kitchen_ai_history_task
from .demos.restaurant import kitchen_rules_engine as kitchen_rules_engine

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
DEMOS_DIR = FRONTEND_DIR / "demos"


class _HttpOnlyStaticFiles(StaticFiles):
    """StaticFiles that silently rejects WebSocket scopes.

    Starlette's stock StaticFiles asserts on any scope where
    `scope["type"] != "http"`, which produces a noisy AssertionError
    traceback in uvicorn whenever a misaddressed WebSocket open falls
    through the router (a Mount matches by path-prefix regardless of
    scope type, so a `ws://host/foo` request to an unmatched path lands
    here instead of returning 404). We swallow the WS scope with a clean
    close so logs stay readable, and the client side just sees the
    socket close immediately.
    """

    async def __call__(self, scope, receive, send):  # type: ignore[override]
        if scope.get("type") == "websocket":
            # 1008 = policy violation. Code is arbitrary — what matters
            # is that we send `websocket.close` and don't raise.
            try:
                msg = await receive()
                if msg.get("type") == "websocket.connect":
                    await send({"type": "websocket.close", "code": 1008})
            except Exception:
                pass
            return
        await super().__call__(scope, receive, send)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup: kick off the background MQTT client (no-op until configured),
    # spin up the AI-Camera rules engine so saved rules start scanning
    # without anyone opening a page, and (re)start the SIP Live Assistant
    # TCP listener if it's enabled in state.
    mqtt_service.start()
    await ai_camera_engine.start_engine()
    sip_live_agent_service.apply_config()
    sip_live_rep_service.apply_config()
    clinic_live_agent_service.apply_config()
    restaurant_live_agent_service.apply_config()
    # Kitchen → AI-History background poller. Self-pauses when no
    # cameras are selected, so this is safe to start eagerly.
    kitchen_ai_history_task.ensure_running()
    # Kitchen → AI-Rules engine. Self-pauses when no rules are active
    # or when Frigate / Gemini are unconfigured.
    kitchen_rules_engine.ensure_running()
    try:
        yield
    finally:
        await restaurant_live_agent_service.stop()
        await clinic_live_agent_service.stop()
        await sip_live_rep_service.stop()
        await sip_live_agent_service.stop()
        await ai_camera_engine.stop_engine()
        mqtt_service.stop()


app = FastAPI(title="PW Demo Master", version="0.6.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Edge auth — HTTP Basic for the master admin app + /demo landing + admin APIs.
#
# The per-vertical demos (clinic, restaurant) already have their own cookie
# session login, so they are deliberately EXEMPT from this layer. /api/health
# is also exempt so Cloudflare / uptime probes can hit it without creds.
#
# Credentials are hardcoded defaults that can be overridden at runtime via
# env vars (PW_MAIN_AUTH_USER / PW_MAIN_AUTH_PASS). On the VM these get set
# in /etc/pwdemo.env which the systemd unit sources. Local dev: leave unset
# and use admin / pwdemo2026.
# ---------------------------------------------------------------------------

_BASIC_AUTH_USER  = os.environ.get("PW_MAIN_AUTH_USER", "admin")
_BASIC_AUTH_PASS  = os.environ.get("PW_MAIN_AUTH_PASS", "pwdemo2026")
_BASIC_AUTH_REALM = "PW Demo Master"

# Disabling explicitly (set both vars to empty string) skips the middleware
# entirely — useful locally on a dev machine where the cred prompt is noise.
_BASIC_AUTH_ENABLED = bool(_BASIC_AUTH_USER) and bool(_BASIC_AUTH_PASS)

_BASIC_AUTH_EXEMPT_PREFIXES = (
    "/api/demo/clinic",
    "/api/demo/restaurant",
    "/demo/clinic",
    "/demo/restaurant",
)


def _is_basic_auth_exempt(path: str) -> bool:
    if path == "/api/health":
        return True
    for p in _BASIC_AUTH_EXEMPT_PREFIXES:
        if path == p or path.startswith(p + "/"):
            return True
    return False


def _check_basic_auth(header: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except Exception:
        return False
    if ":" not in decoded:
        return False
    user, _, password = decoded.partition(":")
    # secrets.compare_digest avoids timing leaks for both halves.
    return (secrets.compare_digest(user,     _BASIC_AUTH_USER) and
            secrets.compare_digest(password, _BASIC_AUTH_PASS))


@app.middleware("http")
async def basic_auth_middleware(request, call_next):
    if not _BASIC_AUTH_ENABLED or _is_basic_auth_exempt(request.url.path):
        return await call_next(request)
    if _check_basic_auth(request.headers.get("authorization", "")):
        return await call_next(request)
    return Response(
        content="Authentication required.\n",
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{_BASIC_AUTH_REALM}"'},
    )

# API routers — each integration gets its own prefix so it can grow independently.
app.include_router(frigate.router,        prefix="/api/frigate",        tags=["frigate"])
app.include_router(homeassistant.router,  prefix="/api/homeassistant",  tags=["homeassistant"])
app.include_router(live_agent.router,     prefix="/api/live-agent",     tags=["live-agent"])
app.include_router(ai_camera.router,      prefix="/api/ai-camera",      tags=["ai-camera"])
app.include_router(ai_camera_rules.router, prefix="/api/ai-camera",     tags=["ai-camera-rules"])
app.include_router(mqtt.router,           prefix="/api/mqtt",           tags=["mqtt"])
app.include_router(sip.router,            prefix="/api/sip",            tags=["sip"])
app.include_router(sip_live_agent.router, prefix="/api/sip-live-agent", tags=["sip-live-agent"])
app.include_router(sip_live_rep.router,   prefix="/api/sip-live-rep",   tags=["sip-live-rep"])

# Per-vertical demos. `/demo` is the 6-card landing page; each vertical
# has a static SPA mount under `/demo/<slug>/` and its API at
# `/api/demo/<slug>/*`. The session cookie lives at path `/` with a
# per-slug name (`pw_demo_clinic`, `pw_demo_restaurant`, …) so verticals
# don't clobber each other in the same browser.
app.include_router(demos_landing.router,    prefix="/demo",                 tags=["demos"])
app.include_router(demos_clinic.router,     prefix="/api/demo/clinic",      tags=["demo-clinic"])
app.include_router(demos_restaurant.router, prefix="/api/demo/restaurant",  tags=["demo-restaurant"])


@app.get("/api/health")
async def app_health() -> dict:
    return {"status": "ok"}


# Per-vertical SPA mounts. These come BEFORE the root `/` static mount so
# `/demo/clinic/assets/...` requests hit the vertical's built bundle, not
# the main admin app's frontend folder.
if (DEMOS_DIR / "clinic").is_dir():
    # A bare GET /demo/clinic (no trailing slash) doesn't match the StaticFiles
    # mount (which only catches /demo/clinic/<sub-path>), so without this
    # explicit redirect the request falls through to the root `/` mount and
    # serves the admin app's index.html by mistake. The vite base is also
    # /demo/clinic/, so the SPA needs the trailing slash for relative paths.
    from fastapi.responses import RedirectResponse  # local — avoids top-import sprawl

    @app.get("/demo/clinic", include_in_schema=False)
    async def _redirect_to_clinic_slash() -> RedirectResponse:
        return RedirectResponse(url="/demo/clinic/", status_code=308)

    app.mount(
        "/demo/clinic",
        _HttpOnlyStaticFiles(directory=str(DEMOS_DIR / "clinic"), html=True),
        name="demo-clinic-spa",
    )

# Same pattern for the Restaurant vertical — bare /demo/restaurant
# redirect + static SPA mount under /demo/restaurant/.
if (DEMOS_DIR / "restaurant").is_dir():
    from fastapi.responses import RedirectResponse  # idempotent import

    @app.get("/demo/restaurant", include_in_schema=False)
    async def _redirect_to_restaurant_slash() -> RedirectResponse:
        return RedirectResponse(url="/demo/restaurant/", status_code=308)

    app.mount(
        "/demo/restaurant",
        _HttpOnlyStaticFiles(directory=str(DEMOS_DIR / "restaurant"), html=True),
        name="demo-restaurant-spa",
    )


# Serve the frontend last so /api/* takes precedence.
app.mount(
    "/",
    _HttpOnlyStaticFiles(directory=str(FRONTEND_DIR), html=True),
    name="frontend",
)


@app.exception_handler(404)
async def spa_fallback(request, exc):  # noqa: ARG001
    from fastapi.responses import JSONResponse

    path = request.url.path
    # Admin-app API paths should 404 as JSON.
    if path.startswith("/api"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    # SPA fallback per vertical — direct GET on /demo/<slug>/dashboard
    # (or any TanStack Router client route) returns that SPA's
    # index.html so the client router takes over.
    if path.startswith("/demo/clinic/"):
        clinic_index = DEMOS_DIR / "clinic" / "index.html"
        if clinic_index.exists():
            return FileResponse(clinic_index)
    if path.startswith("/demo/restaurant/"):
        restaurant_index = DEMOS_DIR / "restaurant" / "index.html"
        if restaurant_index.exists():
            return FileResponse(restaurant_index)
    return FileResponse(FRONTEND_DIR / "index.html")
