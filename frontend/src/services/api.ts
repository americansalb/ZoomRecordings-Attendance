import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  headers: {
    'Content-Type': 'application/json',
  },
})

// Types
export interface Recording {
  id: string
  meeting_id: string
  topic: string
  session_code: string | null
  start_time: string
  duration: number
  host_name: string
  host_email: string
  recording_count: number
  total_size: number
  recording_files: RecordingFile[]
}

export interface RecordingFile {
  id: string
  file_type: string
  file_size: number
  download_url: string
  play_url: string
  recording_type: string
}

export interface Participant {
  name: string
  first_name: string
  last_name: string
  email: string
  attendance_minutes: number
  first_join: string | null
  last_leave: string | null
  is_new?: boolean
  matched_row?: number | null
}

export interface Profile {
  row_number: number
  first_name: string
  last_name: string
  email: string
  attendance: Record<string, number | string>
}

export interface Sheet {
  id: string
  name: string
  session_code: string | null
  url: string
  profile_count?: number
  dates?: string[]
}

export interface DuplicateMatch {
  profile1: { row: number; name: string }
  profile2: { row: number; name: string }
  confidence: number
  reason: string
}

// Recordings API
export const recordingsApi = {
  list: async (params?: { from_date?: string; to_date?: string; search?: string }) => {
    const { data } = await api.get('/recordings', { params })
    return data as { recordings: Recording[]; total: number }
  },

  getParticipants: async (meetingId: string) => {
    const { data } = await api.get(`/recordings/${meetingId}/participants`)
    return data as { participants: Participant[]; total: number }
  },
}

// Attendance API
export const attendanceApi = {
  preview: async (meetingId: string, recordingTitle: string) => {
    const { data } = await api.get(`/attendance/preview/${meetingId}`, {
      params: { recording_title: recordingTitle },
    })
    return data as {
      session_code: string | null
      existing_sheet: { id: string; name: string } | null
      participants: Participant[]
      new_count: number
      existing_count: number
    }
  },

  process: async (meetingId: string, recordingTitle: string, meetingDate: string, spreadsheetId?: string) => {
    const { data } = await api.post('/attendance/process', {
      meeting_id: meetingId,
      recording_title: recordingTitle,
      meeting_date: meetingDate,
      spreadsheet_id: spreadsheetId,
    })
    return data
  },

  update: async (spreadsheetId: string, rowNumber: number, date: string, attendanceMinutes?: number, participationMinutes?: number) => {
    const { data } = await api.post('/attendance/update', {
      spreadsheet_id: spreadsheetId,
      row_number: rowNumber,
      date: date,
      attendance_minutes: attendanceMinutes,
      participation_minutes: participationMinutes,
    })
    return data
  },

  bulkUpdate: async (spreadsheetId: string, date: string, updates: Array<{ row_number: number; attendance_minutes?: number; participation_minutes?: number }>) => {
    const { data } = await api.post('/attendance/bulk-update', {
      spreadsheet_id: spreadsheetId,
      date: date,
      updates: updates,
    })
    return data
  },
}

// Sheets API
export const sheetsApi = {
  list: async () => {
    const { data } = await api.get('/sheets')
    return data as { sheets: Sheet[]; total: number }
  },

  getBySession: async (sessionCode: string) => {
    const { data } = await api.get(`/sheets/${sessionCode}`)
    return data as Sheet
  },

  create: async (sessionCode: string, title?: string) => {
    const { data } = await api.post('/sheets', { session_code: sessionCode, title })
    return data as Sheet
  },

  getData: async (spreadsheetId: string) => {
    const { data } = await api.get(`/sheets/${spreadsheetId}/data`)
    return data as { headers: string[]; rows: string[][]; total_rows: number }
  },
}

// Students API
export const studentsApi = {
  search: async (query: string, sessionCode?: string) => {
    const { data } = await api.get('/students/search', {
      params: { query, session_code: sessionCode },
    })
    return data as { results: (Profile & { session_code: string; spreadsheet_id: string; spreadsheet_name: string })[]; total: number }
  },

  getProfile: async (spreadsheetId: string, rowNumber: number) => {
    const { data } = await api.get(`/students/profile/${spreadsheetId}/${rowNumber}`)
    return data as Profile & {
      summary: {
        total_sessions: number
        total_attendance_minutes: number
        total_participation_minutes: number
        average_attendance: number
      }
    }
  },

  getSessionStudents: async (spreadsheetId: string) => {
    const { data } = await api.get(`/students/session/${spreadsheetId}`)
    return data as { profiles: Profile[]; total: number; dates: string[] }
  },

  findDuplicates: async (spreadsheetId: string) => {
    const { data } = await api.get(`/students/duplicates/${spreadsheetId}`)
    return data as { duplicates: DuplicateMatch[]; total: number }
  },

  merge: async (spreadsheetId: string, keepRow: number, mergeRow: number) => {
    const { data } = await api.post('/students/merge', {
      spreadsheet_id: spreadsheetId,
      keep_row: keepRow,
      merge_row: mergeRow,
    })
    return data
  },

  updateProfile: async (spreadsheetId: string, rowNumber: number, firstName: string, lastName: string, email: string) => {
    const { data } = await api.put('/students/profile', {
      spreadsheet_id: spreadsheetId,
      row_number: rowNumber,
      first_name: firstName,
      last_name: lastName,
      email: email,
    })
    return data
  },
}

export default api
