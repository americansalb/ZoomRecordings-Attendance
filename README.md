# Zoom Attendance Tracker

A web application for tracking attendance from Zoom recordings and managing it in Google Sheets.

## Features

### Admin Features
- **Process Recordings**: Select Zoom cloud recordings and automatically extract attendance
- **Session Management**: Organize attendance by session (extracted from recording titles)
- **Edit Attendance**: Manually adjust attendance and participation minutes
- **Duplicate Detection**: Find and merge duplicate student profiles
- **Google Sheets Integration**: All data synced with Google Sheets

### Student Features
- **Search Profile**: Students can search by name/email to find their records
- **View Attendance**: See attendance history across all sessions
- **Summary Stats**: Total attendance, participation, and averages

## Setup

### Prerequisites
- Python 3.9+
- Node.js 18+
- Zoom Business/Pro account with API access
- Google Cloud project with Sheets API enabled

### 1. Zoom API Setup

1. Go to [Zoom Marketplace](https://marketplace.zoom.us/)
2. Click "Develop" → "Build App"
3. Choose "Server-to-Server OAuth"
4. Fill in app details and note:
   - Account ID
   - Client ID
   - Client Secret
5. Add scopes:
   - `cloud_recording:read:admin`
   - `report:read:admin`
   - `user:read:admin`

### 2. Google Sheets API Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing
3. Enable "Google Sheets API" and "Google Drive API"
4. Create a Service Account:
   - Go to "IAM & Admin" → "Service Accounts"
   - Create service account
   - Download JSON key file
5. Share your Google Drive folder with the service account email

### 3. Environment Setup

```bash
# Backend
cd backend
cp .env.example .env
# Edit .env with your credentials

# Place your Google service account JSON as credentials.json
```

### 4. Local Development

```bash
# Backend
cd backend
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# Frontend (new terminal)
cd frontend
npm install
npm run dev
```

Visit http://localhost:3000

## Recording Title Format

For automatic session detection, recordings should be titled:
```
Session XXX. [Description]
```

Example: `Session 127. Mondays, Wednesdays, and Fridays, (Night | November 10th to December 22nd, 2025)`

The 3-digit number after "Session" is used to match/create Google Sheets.

## Deployment (Render)

1. Push code to GitHub
2. Go to [Render Dashboard](https://dashboard.render.com/)
3. Click "New" → "Blueprint"
4. Connect your GitHub repo
5. Render will use `render.yaml` to set up services
6. Add environment variables in Render dashboard

## API Endpoints

### Recordings
- `GET /api/recordings` - List all recordings
- `GET /api/recordings/{id}/participants` - Get participant list

### Attendance
- `POST /api/attendance/process` - Process attendance from recording
- `POST /api/attendance/update` - Update individual attendance
- `GET /api/attendance/preview/{id}` - Preview before processing

### Students
- `GET /api/students/search?query=` - Search students
- `GET /api/students/profile/{sheet_id}/{row}` - Get student profile
- `GET /api/students/duplicates/{sheet_id}` - Find duplicates
- `POST /api/students/merge` - Merge duplicate profiles

### Sheets
- `GET /api/sheets` - List all session sheets
- `GET /api/sheets/{session_code}` - Get sheet by session
- `POST /api/sheets` - Create new session sheet

## Tech Stack

- **Backend**: Python, FastAPI, Google API Client
- **Frontend**: React, TypeScript, TailwindCSS, React Query
- **Deployment**: Render
