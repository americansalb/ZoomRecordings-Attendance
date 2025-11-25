from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv
import os
from pathlib import Path

from routes import recordings, attendance, students, sheets

load_dotenv()

app = FastAPI(
    title="Zoom Attendance Tracker",
    description="Track attendance from Zoom recordings and manage Google Sheets",
    version="1.0.0"
)

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
