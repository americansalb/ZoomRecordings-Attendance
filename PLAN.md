# Student Record Search Feature - Implementation Plan

## Overview
Replace the current open search with a new student lookup feature that requires First Name + Session #, masks last names in results, and displays Student ID prominently with styled formatting.

## Changes Required

### 1. Backend - New Search Endpoint (`backend/routes/students.py`)

Add a new endpoint `GET /students/lookup`:
- **Required parameters**: `first_name` (string), `session_code` (string, 3 digits)
- **Behavior**:
  - Searches ONLY within the specified session's Summary tab
  - Matches students whose first name starts with or contains the search term (case-insensitive)
  - Returns masked results
- **Response format**:
```python
{
  "results": [
    {
      "row_number": 5,
      "session_code": "129",
      "display_name": "Maria O**r",      # Masked: full first + first letter + asterisks + last letter
      "first_name": "Maria",              # Needed for profile lookup
      "student_id_preview": "123**"       # First 3 digits + ** (for preview)
    }
  ],
  "total": 2
}
```

**Name masking logic**:
- Full first name
- Space
- First letter of last name
- Asterisks for middle letters (count = len(last_name) - 2)
- Last letter of last name
- Example: "Kevin Thakkar" → "Kevin T*****r"

### 2. Frontend - New Search Page (`frontend/src/pages/student/Search.tsx`)

Replace current search with new form:
- **Two required input fields**:
  1. First Name (text input)
  2. Session # (text input, 3 digits)
- **Help tooltip** for Session #: "You can find your Session # in your registration email and at the top of Google Classroom"
- **Submit button** - disabled until both fields have valid input
- **Results display**:
  - Show masked names as clickable cards
  - Each card shows: masked name (e.g., "Maria O**r")
  - Click navigates to detail view

### 3. Frontend - Updated Detail View (`frontend/src/pages/student/SummaryProfile.tsx`)

Modify the profile header section:
- **Make Student ID prominent**:
  - Large, bold text
  - Position it prominently in the header
- **Student ID styling** (always 5 digits):
  - First 3 digits: Dark teal (`#0D9488` or `teal-600`)
  - Last 2 digits: Light teal (`#5EEAD4` or `teal-300`)
  - Large font, bold
  - Example: `12345` displays as `123` in dark teal + `45` in light teal
- **Remove/hide** elements that might reveal full name to others viewing over shoulder (optional)

### 4. Frontend - API Service Update (`frontend/src/services/api.ts`)

Add new API method:
```typescript
lookup: async (firstName: string, sessionCode: string) => {
  const { data } = await api.get('/students/lookup', {
    params: { first_name: firstName, session_code: sessionCode },
  })
  return data as {
    results: Array<{
      row_number: number
      session_code: string
      display_name: string
      first_name: string
      student_id_preview: string
    }>
    total: number
  }
}
```

## File Changes Summary

| File | Action |
|------|--------|
| `backend/routes/students.py` | Add `GET /students/lookup` endpoint with masking logic |
| `frontend/src/pages/student/Search.tsx` | Replace with new two-field form, masked results |
| `frontend/src/pages/student/SummaryProfile.tsx` | Redesign header with prominent styled Student ID |
| `frontend/src/services/api.ts` | Add `lookup()` method |

## UI Mockup

### Search Page
```
┌─────────────────────────────────────────────┐
│          Find Your Attendance               │
│                                             │
│  First Name: [________________]             │
│                                             │
│  Session #:  [___] (?)                      │
│  (?) = "Find in registration email or      │
│         at top of Google Classroom"         │
│                                             │
│           [Search]                          │
└─────────────────────────────────────────────┘
```

### Results
```
┌─────────────────────────────────────────────┐
│  Found 2 results                            │
│                                             │
│  ┌───────────────────────────────────────┐  │
│  │  Maria O**r                           │  │
│  └───────────────────────────────────────┘  │
│                                             │
│  ┌───────────────────────────────────────┐  │
│  │  Maria S*****z                        │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

### Detail View Header
```
┌─────────────────────────────────────────────┐
│  [Avatar]                                   │
│                                             │
│  Student ID                                 │
│  ┌─────────────────┐                        │
│  │ 123 45          │  (large, bold)         │
│  │ ^^^dark ^^light │  (teal colors)         │
│  └─────────────────┘                        │
│                                             │
│  Maria Ozar                                 │
│  Session 129                                │
└─────────────────────────────────────────────┘
```

## Implementation Order

1. Backend: Add `/students/lookup` endpoint with name masking
2. Frontend: Update `api.ts` with `lookup()` method
3. Frontend: Rewrite `Search.tsx` with new form and masked results
4. Frontend: Update `SummaryProfile.tsx` with prominent styled Student ID
5. Test and commit
