from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import os
import logging
from pathlib import Path

from routes import recordings, attendance, students, sheets, mappings, accounts, proctoring

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Zoom Attendance Tracker",
    description="Track attendance from Zoom recordings and manage Google Sheets",
    version="1.0.0"
)


@app.on_event("startup")
async def startup_event():
    """Log configuration status on startup"""
    print("=" * 50, flush=True)
    print("ZOOM ATTENDANCE TRACKER - STARTUP", flush=True)
    print("=" * 50, flush=True)

    # Check Zoom credentials
    zoom_account = os.getenv("ZOOM_ACCOUNT_ID")
    zoom_client = os.getenv("ZOOM_CLIENT_ID")
    zoom_secret = os.getenv("ZOOM_CLIENT_SECRET")
    print(f"ZOOM_ACCOUNT_ID: {'SET' if zoom_account else 'MISSING'}", flush=True)
    print(f"ZOOM_CLIENT_ID: {'SET' if zoom_client else 'MISSING'}", flush=True)
    print(f"ZOOM_CLIENT_SECRET: {'SET' if zoom_secret else 'MISSING'}", flush=True)

    # Check Google credentials
    google_email = os.getenv("GOOGLE_CLIENT_EMAIL")
    google_key = os.getenv("GOOGLE_PRIVATE_KEY")
    spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID") or os.getenv("GOOGLE_SHEET_ID")
    roster_id = os.getenv("ROSTER_SPREADSHEET_ID")
    print(f"GOOGLE_CLIENT_EMAIL: {'SET' if google_email else 'MISSING'}", flush=True)
    print(f"GOOGLE_PRIVATE_KEY: {'SET' if google_key else 'MISSING'}", flush=True)
    print(f"GOOGLE_SPREADSHEET_ID: {'SET' if spreadsheet_id else 'MISSING'}", flush=True)
    print(f"ROSTER_SPREADSHEET_ID: {'SET' if roster_id else 'NOT SET (roster matching disabled)'}", flush=True)

    # Log registered routes
    print("=" * 50, flush=True)
    print("REGISTERED ROUTES:", flush=True)
    for route in app.routes:
        if hasattr(route, 'path') and hasattr(route, 'methods'):
            print(f"  {route.methods} {route.path}", flush=True)
        elif hasattr(route, 'path'):
            print(f"  MOUNT {route.path}", flush=True)
    print("=" * 50, flush=True)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routers
app.include_router(accounts.router, prefix="/api/accounts", tags=["Accounts"])
app.include_router(recordings.router, prefix="/api/recordings", tags=["Recordings"])
app.include_router(attendance.router, prefix="/api/attendance", tags=["Attendance"])
app.include_router(students.router, prefix="/api/students", tags=["Students"])
app.include_router(sheets.router, prefix="/api/sheets", tags=["Sheets"])
app.include_router(mappings.router, prefix="/api/mappings", tags=["Mappings"])
app.include_router(proctoring.router, prefix="/api", tags=["Proctoring"])


@app.get("/api/health")
async def health_check():
    print("[HEALTH] Health check called", flush=True)
    return {"status": "healthy"}


@app.get("/api/test")
async def test_endpoint():
    print("[TEST] Test endpoint called", flush=True)
    return {"test": "working", "message": "API routes are functioning"}


# Serve static frontend files
static_dir = Path(__file__).parent.parent / "frontend" / "dist"
if static_dir.exists():
    # Mount assets directory for JS/CSS bundles
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    # Catch-all for frontend SPA - must check it's not an API route
    @app.get("/{full_path:path}")
    async def serve_frontend(request: Request, full_path: str):
        # Don't intercept API routes
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API route not found")

        # Serve actual files if they exist
        file_path = static_dir / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)

        # Serve index.html for SPA routing
        return FileResponse(static_dir / "index.html")
