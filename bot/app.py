"""
Bot service HTTP app -- implements the TUTOR_BOT.md contract the Phase 1 backend
drives:

    POST   /bots                      -> {runtime_id}
    DELETE /bots/{runtime_id}
    POST   /bots/{runtime_id}/messages

It also serves the Zoom Web SDK client page (static/) with the cross-origin
isolation headers the SDK needs, and verifies the shared secret on inbound
control calls when configured.

`build_app(...)` takes injectable factories so the orchestration is testable
with fakes; `app = build_app()` is the module-level instance uvicorn runs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Config, load_config
from .manager import BotManager
from .meeting_client import build_meeting_client
from .storage import build_storage
from .backend_client import BackendClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BUILD = "capture-4"


class MessageIn(BaseModel):
    channel: str = "public"
    text: str
    to_participant_id: Optional[str] = None


class CaptureConfigIn(BaseModel):
    interval_seconds: Optional[int] = None
    store_images: Optional[bool] = None


def build_app(
    config: Optional[Config] = None,
    *,
    backend=None,
    client_factory: Optional[Callable] = None,
    storage_factory: Optional[Callable] = None,
) -> FastAPI:
    config = config or load_config()
    backend = backend or BackendClient(config.backend_url, config.bot_shared_secret)
    manager = BotManager(
        config, backend,
        client_factory=client_factory or build_meeting_client,
        storage_factory=storage_factory or build_storage,
    )

    app = FastAPI(title="Live Tutor Bot", version="1.0.0")
    app.state.config = config
    app.state.manager = manager

    def _check_secret(provided: Optional[str]) -> None:
        if config.bot_shared_secret and provided != config.bot_shared_secret:
            raise HTTPException(status_code=401, detail="Invalid bot secret")

    @app.middleware("http")
    async def _cross_origin_isolation(request: Request, call_next):
        """The Zoom Web SDK needs SharedArrayBuffer -> cross-origin isolation."""
        resp = await call_next(request)
        if request.url.path.startswith(("/static", "/lib")):
            resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
            resp.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
        return resp

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # The SDK fetches its audio/video wasm and workers once a meeting
    # starts. Depending on version it resolves them relative to its own
    # script or from /lib at the root, so serve both. Cheap insurance
    # against the join getting further and then dying on a 404 for a
    # worker file, which reads as a totally different bug.
    vendor_lib = static_dir / "vendor" / "lib"
    if vendor_lib.exists():
        app.mount("/lib", StaticFiles(directory=str(vendor_lib)), name="zoomsdk-lib")

    @app.on_event("shutdown")
    async def _shutdown():
        await manager.shutdown()

    @app.get("/healthz")
    async def healthz():
        # BUILD lets the console prove which code a deploy is actually
        # running, instead of inferring it from behaviour.
        return {"ok": True, "sdk_configured": bool(config.sdk_key), "build": BUILD}

    @app.get("/bots")
    async def list_bots(x_tutor_bot_secret: Optional[str] = Header(default=None)):
        """Which bots are actually live in this process.

        The authoritative answer to "is the bot still in that meeting". A
        control plane that only reads its own records cannot tell a live bot
        from one whose container was redeployed underneath it.
        """
        _check_secret(x_tutor_bot_secret)
        return {"bots": manager.list_sessions()}

    @app.post("/bots")
    async def join_meeting(request: Request, x_tutor_bot_secret: Optional[str] = Header(default=None)):
        _check_secret(x_tutor_bot_secret)
        payload = await request.json()
        try:
            runtime_id = await manager.join(payload)
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.exception("join failed")
            raise HTTPException(status_code=500, detail=f"join failed: {e}")
        return {"runtime_id": runtime_id}

    @app.delete("/bots/{runtime_id}")
    async def leave_meeting(runtime_id: str, x_tutor_bot_secret: Optional[str] = Header(default=None)):
        _check_secret(x_tutor_bot_secret)
        await manager.leave(runtime_id)
        return JSONResponse({"ok": True})

    @app.get("/bots/{runtime_id}/screenshot")
    async def bot_screenshot(runtime_id: str,
                             x_tutor_bot_secret: Optional[str] = Header(default=None)):
        """PNG of the bot's whole browser page.

        The evidence view: when capture or camera state is disputed, this is
        what the bot is actually looking at, not a description of it.
        """
        _check_secret(x_tutor_bot_secret)
        try:
            session = manager._require(runtime_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown runtime_id")
        shot = await session.client.page_screenshot()
        if shot is None:
            raise HTTPException(status_code=503, detail="no page to screenshot")
        return Response(content=shot, media_type="image/png")

    @app.get("/bots/{runtime_id}/face-check/{user_id}")
    async def face_check(runtime_id: str, user_id: str,
                         x_tutor_bot_secret: Optional[str] = Header(default=None)):
        """One frame, checked for a face, with a box drawn around every hit.

        The calibration view: the exact production detector on the exact
        production capture, made visible, so "what counted as a face" is
        something an operator can look at instead of guess about.
        """
        _check_secret(x_tutor_bot_secret)
        try:
            session = manager._require(runtime_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown runtime_id")
        frame = await session.client.capture_user(user_id)
        if frame is None:
            raise HTTPException(
                status_code=404,
                detail="No video tile is rendered for this person right now.")
        from .face import face_check_annotated
        found, png = await asyncio.to_thread(face_check_annotated, frame)
        if png is None:
            raise HTTPException(status_code=500, detail="could not analyse the frame")
        return Response(content=png, media_type="image/png",
                        headers={"X-Face-Found": "true" if found else "false"})

    @app.get("/bots/{runtime_id}/diagnostics")
    async def bot_diagnostics(runtime_id: str,
                              x_tutor_bot_secret: Optional[str] = Header(default=None)):
        """What the Zoom SDK itself reports, unmapped.

        Camera state is the field the whole participation rule rests on. When
        it disagrees with what someone sees in their own Zoom window, this is
        the only way to tell a bad mapping from a bad SDK reading.
        """
        _check_secret(x_tutor_bot_secret)
        try:
            session = manager._require(runtime_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown runtime_id")
        return await session.client.diagnostics()

    @app.post("/bots/{runtime_id}/capture")
    async def capture_now(runtime_id: str,
                          x_tutor_bot_secret: Optional[str] = Header(default=None)):
        """Run one attendance sweep right now, without waiting for the interval."""
        _check_secret(x_tutor_bot_secret)
        try:
            recorded = await manager.capture_now(runtime_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown runtime_id")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"capture failed: {e}")
        return {"ok": True, "recorded": recorded}

    @app.patch("/bots/{runtime_id}/capture")
    async def update_capture(runtime_id: str, body: CaptureConfigIn,
                             x_tutor_bot_secret: Optional[str] = Header(default=None)):
        """Retune a running attendance loop without a dismiss and re-summon."""
        _check_secret(x_tutor_bot_secret)
        try:
            cfg = manager.set_capture_config(
                runtime_id,
                interval_seconds=body.interval_seconds,
                store_images=body.store_images,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown runtime_id")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"reconfigure failed: {e}")
        return {"ok": True, **cfg}

    @app.post("/bots/{runtime_id}/messages")
    async def send_message(runtime_id: str, body: MessageIn,
                           x_tutor_bot_secret: Optional[str] = Header(default=None)):
        _check_secret(x_tutor_bot_secret)
        try:
            await manager.send(runtime_id, body.channel, body.text, body.to_participant_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown runtime_id")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"send failed: {e}")
        return JSONResponse({"ok": True})

    return app


app = build_app()
