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

export interface Session {
  name: string
  session_code: string
  sheet_id: number
  spreadsheet_url?: string
  profile_count?: number
  dates?: string[]
}

export interface DuplicateMatch {
  profile1: { row: number; name: string }
  profile2: { row: number; name: string }
  confidence: number
  reason: string
}

export interface NameMapping {
  row_number?: number
  zoom_name: string
  student_id: string
  first_name: string
  last_name: string
  session_code: string
  created_at?: string
}

export interface RosterStudent {
  student_id: string
  first_name: string
  last_name: string
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
      existing_tab: { name: string; sheet_id: number } | null
      participants: Participant[]
      new_count: number
      existing_count: number
    }
  },

  process: async (meetingId: string, recordingTitle: string, meetingDate: string) => {
    const { data } = await api.post('/attendance/process', {
      meeting_id: meetingId,
      recording_title: recordingTitle,
      meeting_date: meetingDate,
    })
    return data
  },

  update: async (sessionCode: string, rowNumber: number, date: string, attendanceMinutes?: number, participationMinutes?: number) => {
    const { data } = await api.post('/attendance/update', {
      session_code: sessionCode,
      row_number: rowNumber,
      date: date,
      attendance_minutes: attendanceMinutes,
      participation_minutes: participationMinutes,
    })
    return data
  },

  bulkUpdate: async (sessionCode: string, date: string, updates: Array<{ row_number: number; attendance_minutes?: number; participation_minutes?: number }>) => {
    const { data } = await api.post('/attendance/bulk-update', {
      session_code: sessionCode,
      date: date,
      updates: updates,
    })
    return data
  },
}

// Sheets/Sessions API
export const sheetsApi = {
  list: async () => {
    const { data } = await api.get('/sheets')
    return data as { sessions: Session[]; total: number; spreadsheet_url: string }
  },

  getBySession: async (sessionCode: string) => {
    const { data } = await api.get(`/sheets/${sessionCode}`)
    return data as Session
  },

  create: async (sessionCode: string) => {
    const { data } = await api.post('/sheets', { session_code: sessionCode })
    return data as Session
  },

  getData: async (sessionCode: string) => {
    const { data } = await api.get(`/sheets/${sessionCode}/data`)
    return data as { headers: string[]; rows: string[][]; total_rows: number }
  },
}

// Students API
export const studentsApi = {
  search: async (query: string, sessionCode?: string) => {
    const { data } = await api.get('/students/search', {
      params: { query, session_code: sessionCode },
    })
    return data as { results: (Profile & { session_code: string; session_name: string })[]; total: number }
  },

  getProfile: async (sessionCode: string, rowNumber: number) => {
    const { data } = await api.get(`/students/profile/${sessionCode}/${rowNumber}`)
    return data as Profile & {
      session_code: string
      summary: {
        total_sessions: number
        total_attendance_minutes: number
        total_participation_minutes: number
        average_attendance: number
      }
    }
  },

  getSessionStudents: async (sessionCode: string) => {
    const { data } = await api.get(`/students/session/${sessionCode}`)
    return data as { profiles: Profile[]; total: number; dates: string[] }
  },

  findDuplicates: async (sessionCode: string) => {
    const { data } = await api.get(`/students/duplicates/${sessionCode}`)
    return data as { duplicates: DuplicateMatch[]; total: number }
  },

  merge: async (sessionCode: string, keepRow: number, mergeRow: number) => {
    const { data } = await api.post('/students/merge', {
      session_code: sessionCode,
      keep_row: keepRow,
      merge_row: mergeRow,
    })
    return data
  },

  updateProfile: async (sessionCode: string, rowNumber: number, firstName: string, lastName: string, email: string) => {
    const { data } = await api.put('/students/profile', {
      session_code: sessionCode,
      row_number: rowNumber,
      first_name: firstName,
      last_name: lastName,
      email: email,
    })
    return data
  },
}

// Name Mappings API
export const mappingsApi = {
  list: async (sessionCode?: string) => {
    const { data } = await api.get('/mappings', {
      params: sessionCode ? { session_code: sessionCode } : undefined,
    })
    return data as { mappings: NameMapping[]; total: number }
  },

  create: async (mapping: Omit<NameMapping, 'row_number' | 'created_at'>) => {
    const { data } = await api.post('/mappings', mapping)
    return data as { success: boolean; mapping: NameMapping }
  },

  delete: async (zoomName: string) => {
    const { data } = await api.delete(`/mappings/${encodeURIComponent(zoomName)}`)
    return data as { success: boolean; deleted: string }
  },

  getRoster: async (sessionCode: string) => {
    const { data } = await api.get(`/mappings/roster/${sessionCode}`)
    return data as { roster: RosterStudent[]; total: number; session_code: string }
  },
}

export default api
