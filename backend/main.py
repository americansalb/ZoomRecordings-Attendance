from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import os
import logging
from pathlib import Path

from routes import recordings, attendance, students, sheets

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
    logger.info("=" * 50)
    logger.info("ZOOM ATTENDANCE TRACKER - STARTUP")
    logger.info("=" * 50)

    # Check Zoom credentials
    zoom_account = os.getenv("ZOOM_ACCOUNT_ID")
    zoom_client = os.getenv("ZOOM_CLIENT_ID")
    zoom_secret = os.getenv("ZOOM_CLIENT_SECRET")
    logger.info(f"ZOOM_ACCOUNT_ID: {'SET' if zoom_account else 'MISSING'}")
    logger.info(f"ZOOM_CLIENT_ID: {'SET' if zoom_client else 'MISSING'}")
    logger.info(f"ZOOM_CLIENT_SECRET: {'SET' if zoom_secret else 'MISSING'}")

    # Check Google credentials
    google_email = os.getenv("GOOGLE_CLIENT_EMAIL")
    google_key = os.getenv("GOOGLE_PRIVATE_KEY")
    spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID") or os.getenv("GOOGLE_SHEET_ID")
    logger.info(f"GOOGLE_CLIENT_EMAIL: {'SET' if google_email else 'MISSING'}")
    logger.info(f"GOOGLE_PRIVATE_KEY: {'SET' if google_key else 'MISSING'}")
    logger.info(f"GOOGLE_SPREADSHEET_ID: {'SET' if spreadsheet_id else 'MISSING'} (checked GOOGLE_SPREADSHEET_ID and GOOGLE_SHEET_ID)")

    logger.info("=" * 50)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routers
app.include_router(recordings.router, prefix="/api/recordings", tags=["Recordings"])
app.include_router(attendance.router, prefix="/api/attendance", tags=["Attendance"])
app.include_router(students.router, prefix="/api/students", tags=["Students"])
app.include_router(sheets.router, prefix="/api/sheets", tags=["Sheets"])


@app.get("/api/health")
async def health_check():
    return {"status": "healthy"}


# Serve static frontend files
static_dir = Path(__file__).parent.parent / "frontend" / "dist"
if static_dir.exists():
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(request: Request, full_path: str):
        # Serve index.html for all non-API routes (SPA routing)
        file_path = static_dir / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(static_dir / "index.html")
