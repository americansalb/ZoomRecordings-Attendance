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
import os
from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .capture import CaptureLoop
from .config import Config, load_config
from .manager import BotManager
from .meeting_client import build_meeting_client
from .storage import build_storage
from .backend_client import BackendClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BUILD = "capture-48"


class MessageIn(BaseModel):
    channel: str = "public"
    text: str
    to_participant_id: Optional[str] = None


class CaptureConfigIn(BaseModel):
    interval_seconds: Optional[int] = None
    store_images: Optional[bool] = None
    room_snapshot_seconds: Optional[int] = None
    student_photo_seconds: Optional[int] = None


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

    @app.on_event("startup")
    async def _snapshot_janitor():
        """Delete room and face pictures older than the retention window.

        Off by default (owner's decision, 2026-09-04: the pictures are kept
        for good). Set BOT_IMAGE_RETENTION_DAYS to a positive number of days
        to turn automatic deletion back on. Even then it refuses to run
        without a configured parent folder: the janitor must never roam
        beyond the fence of our own snapshot folder.
        """
        days = int(os.getenv("BOT_IMAGE_RETENTION_DAYS", "0") or 0)
        if days <= 0 or not config.drive_folder_id:
            return

        async def run():
            from .storage import purge_drive_older_than
            while True:
                try:
                    n = await asyncio.to_thread(
                        purge_drive_older_than, config.drive_folder_id, days)
                    if n:
                        logger.info("[JANITOR] deleted %d expired snapshot files and folders", n)
                except Exception as e:
                    logger.warning("[JANITOR] retention purge failed: %s", e)
                await asyncio.sleep(24 * 3600)

        asyncio.create_task(run())

    @app.on_event("shutdown")
    async def _shutdown():
        await manager.shutdown()
        closer = getattr(backend, "aclose", None)
        if closer is not None:
            await closer()

    @app.get("/healthz")
    async def healthz():
        # BUILD lets the console prove which code a deploy is actually
        # running, instead of inferring it from behaviour. memory is the
        # container's own meter, live, so nobody has to guess how close to
        # the kill line a class is running: 0.85 throttles, at 1.0 the
        # platform kills the container.
        # memory is the working set (held memory minus the file cache the
        # kernel reclaims first); memory_with_cache is the raw cgroup
        # number, shown so the two can be compared on a live machine.
        # drive says whether saved pictures (the whole-class grid and the
        # per-student photos) can actually land: both a destination folder
        # and service-account credentials have to be set on this machine.
        # Without it the pictures are taken and thrown away, so the console
        # can stop guessing why a Drive folder stays empty.
        drive_creds = bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
                           or os.getenv("GOOGLE_CLIENT_EMAIL"))
        return {"ok": True, "sdk_configured": bool(config.sdk_key), "build": BUILD,
                "memory": round(CaptureLoop.memory_fraction(), 3),
                "memory_with_cache": round(CaptureLoop.memory_fraction(with_cache=True), 3),
                "drive": {"folder": bool(config.drive_folder_id), "credentials": drive_creds,
                          "ready": bool(config.drive_folder_id) and drive_creds}}

    @app.get("/memz")
    async def memz():
        """Where the memory goes, per process. The budgeting view: it names
        the real consumer (the browser, its renderer, the Python service)
        so a memory fix is aimed, not guessed. Public like /healthz: it
        carries process names and sizes, never user data, and being able to
        read it from anywhere is the point."""
        try:
            data = CaptureLoop.memory_breakdown()
        except Exception as e:
            data = {"error": str(e)}
        data["fraction_working_set"] = round(CaptureLoop.memory_fraction(), 3)
        data["fraction_with_cache"] = round(CaptureLoop.memory_fraction(with_cache=True), 3)
        # Per live session: is Zoom's camera signal still arriving? With the
        # lookout's video rendering cut to one tile, a rising count is the
        # proof the cut cost nothing the record depends on.
        sessions = []
        for info in manager.list_sessions():
            rid = info.get("runtime_id") if isinstance(info, dict) else None
            if not rid:
                continue
            try:
                diag = await manager._require(rid).client.diagnostics()
                sessions.append({
                    "runtime_id": rid,
                    "lookout": diag.get("lookout"),
                    "participants": len(diag.get("raw") or []),
                    "camera_signal_total": diag.get("cameraSignalTotal"),
                    "last_camera_signal_at": diag.get("lastCameraSignalAt"),
                    "page_build": diag.get("pageBuild"),
                })
            except Exception as e:
                sessions.append({"runtime_id": rid, "error": str(e)[:120]})
        data["sessions"] = sessions
        return data

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
        data = await session.client.diagnostics()
        # The container's own memory meter rides along. Face checking is
        # governed by it, so the console can say "faces are waiting because
        # memory is at 91 percent" instead of showing a silent zero.
        try:
            data["memory"] = round(CaptureLoop.memory_fraction(), 3)
        except Exception:
            pass
        return data

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
                room_snapshot_seconds=body.room_snapshot_seconds,
                student_photo_seconds=body.student_photo_seconds,
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
            delivered = await manager.send(runtime_id, body.channel, body.text,
                                           body.to_participant_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown runtime_id")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"send failed: {e}")
        # delivered means Zoom echoed the message back, the only proof it was
        # actually distributed. ok alone only ever meant "the SDK took it".
        return JSONResponse({"ok": True, "delivered": bool(delivered)})

    return app


app = build_app()
