from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os

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
    allow_origins=[os.getenv("FRONTEND_URL", "http://localhost:3000")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(recordings.router, prefix="/api/recordings", tags=["Recordings"])
app.include_router(attendance.router, prefix="/api/attendance", tags=["Attendance"])
app.include_router(students.router, prefix="/api/students", tags=["Students"])
app.include_router(sheets.router, prefix="/api/sheets", tags=["Sheets"])


@app.get("/")
async def root():
    return {"message": "Zoom Attendance Tracker API", "status": "running"}


@app.get("/health")
async def health_check():
    return {"status": "healthy"}
