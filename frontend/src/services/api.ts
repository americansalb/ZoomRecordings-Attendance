import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  headers: {
    'Content-Type': 'application/json',
  },
})

// Types
export interface ZoomAccount {
  id: string
  name: string
}

export interface ZoomUser {
  id: string
  email: string
  first_name: string
  last_name: string
  display_name: string
  type: number
  status: string
}

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

export interface SummaryStudent {
  row_number: number
  student_id: string
  first_name: string
  last_name: string
  known_zoom_names: string[]
  attendance: Record<string, number>
}

// Accounts API
export const accountsApi = {
  list: async () => {
    const { data } = await api.get('/accounts')
    return data as { accounts: ZoomAccount[]; total: number }
  },

  listUsers: async (account_id?: string) => {
    const { data } = await api.get('/accounts/users', {
      params: account_id ? { account_id } : undefined
    })
    return data as { users: ZoomUser[]; total: number }
  },
}

// Recordings API
export const recordingsApi = {
  list: async (params?: { from_date?: string; to_date?: string; search?: string; account_id?: string; user_id?: string }) => {
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
    // Double URL encode meeting ID to handle UUIDs with / and == characters
    const encodedMeetingId = encodeURIComponent(encodeURIComponent(meetingId))
    const { data } = await api.get(`/attendance/preview/${encodedMeetingId}`, {
      params: { recording_title: recordingTitle },
    })
    return data as {
      session_code: string | null
      existing_tab: { name: string; sheet_id: number } | null
      participants: Participant[]
      new_count: number
      existing_count: number
      detected_start_time: string | null
      detected_duration: number | null
      detection_source: string | null
      detection_warnings: string[]
    }
  },

  process: async (meetingId: string, recordingTitle: string, meetingDate: string, meetingDurationMinutes?: number, meetingStartTime?: string, numberOfSegments?: number) => {
    const { data } = await api.post('/attendance/process', {
      meeting_id: meetingId,
      recording_title: recordingTitle,
      meeting_date: meetingDate,
      meeting_duration_minutes: meetingDurationMinutes || undefined,
      meeting_start_time: meetingStartTime || undefined,
      number_of_segments: numberOfSegments || undefined,
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

  getSummary: async (sessionCode: string, regenerate: boolean = false) => {
    const { data } = await api.get(`/sheets/${sessionCode}/summary`, {
      params: regenerate ? { regenerate: true } : undefined,
    })
    return data as {
      session_code: string
      students: SummaryStudent[]
      dates: string[]
      total: number
      spreadsheet_url: string
    }
  },

  regenerateSummary: async (sessionCode: string) => {
    const { data } = await api.post(`/sheets/${sessionCode}/summary/regenerate`)
    return data as {
      success: boolean
      session_code: string
      students: number
      dates: string[]
      summary_tab: { name: string; sheet_id: number; session_code: string }
    }
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

  // Summary-based search (uses canonical roster names, searches Zoom names too)
  searchSummary: async (query: string, sessionCode?: string) => {
    const { data } = await api.get('/students/summary/search', {
      params: { query, session_code: sessionCode },
    })
    return data as {
      results: (SummaryStudent & { session_code: string; session_name: string })[]
      total: number
    }
  },

  // Summary-based profile (shows canonical name with known Zoom names)
  getSummaryProfile: async (sessionCode: string, rowNumber: number) => {
    const { data } = await api.get(`/students/summary/profile/${sessionCode}/${rowNumber}`)
    return data as SummaryStudent & {
      session_code: string
      dates: string[]
      summary: {
        total_sessions: number
        total_attendance_minutes: number
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

// Proctoring Types
export interface ProctorJobStatus {
  job_id: string
  status: 'pending' | 'processing' | 'completed' | 'failed'
  progress: number
  message: string
  result?: ProctorResult
  error?: string
}

export interface ProctorResult {
  recording_id: string
  session_code: string
  meeting_date: string
  total_duration_minutes: number
  frames_analyzed: number
  report_path: string
  screenshots_dir: string
  participants: ProctorParticipantResult[]
}

export interface ProctorParticipantResult {
  name: string
  visibility_percentage: number
  violation_count: number
  total_violation_minutes: number
  issues: Record<string, number>
}

export interface ProctorWarningDocument {
  success: boolean
  has_violations: boolean
  participant_name: string
  session_code?: string
  meeting_date?: string
  meeting_duration_minutes?: number
  visibility_percentage?: number
  total_violation_minutes?: number
  violation_count?: number
  violations?: Array<{
    type: string
    start_time: string
    end_time: string
    duration_minutes: number
  }>
  screenshots?: Array<{
    timestamp: string
    data: string
    filename: string
  }>
  warning_text?: string
  message?: string
}

// Proctoring API
export const proctorApi = {
  startProcessing: async (
    meetingId: string,
    recordingTitle: string,
    sessionCode: string,
    meetingDate: string,
    videoUrl: string,
    participantNames: string[],
    gridLayout?: [number, number],
    sampleInterval?: number
  ) => {
    const { data } = await api.post('/proctor/process', {
      meeting_id: meetingId,
      recording_title: recordingTitle,
      session_code: sessionCode,
      meeting_date: meetingDate,
      video_url: videoUrl,
      participant_names: participantNames,
      grid_layout: gridLayout || null,
      sample_interval: sampleInterval || 30.0,
    })
    return data as { success: boolean; job_id: string; message: string }
  },

  getJobStatus: async (jobId: string) => {
    const { data } = await api.get(`/proctor/status/${jobId}`)
    return data as ProctorJobStatus
  },

  getJobResults: async (jobId: string) => {
    const { data } = await api.get(`/proctor/results/${jobId}`)
    return data as { success: boolean; job_id: string; result: ProctorResult }
  },

  generateWarning: async (jobId: string, participantName: string, minViolationMinutes?: number) => {
    const { data } = await api.post('/proctor/warning', {
      job_id: jobId,
      participant_name: participantName,
      min_violation_minutes: minViolationMinutes || 1.0,
    })
    return data as ProctorWarningDocument
  },

  listJobs: async () => {
    const { data } = await api.get('/proctor/jobs')
    return data as {
      jobs: Array<{
        job_id: string
        status: string
        progress: number
        message: string
        session_code: string
        meeting_date: string
      }>
      total: number
    }
  },
}

// Video Upload Types
export interface VideoPreviewResponse {
  duration_seconds: number
  duration_formatted: string
  width?: number
  height?: number
  size_bytes?: number
}

export interface AutoTrimResponse {
  start_time: number
  end_time: number
  scheduled_start?: string
  scheduled_end?: string
  message: string
}

export interface UploadJobStatus {
  job_id: string
  status: 'pending' | 'downloading' | 'trimming' | 'uploading' | 'completed' | 'failed'
  progress: number
  message: string
  result?: {
    file_id: string
    file_name: string
    web_view_link: string
    session_code: string
    day_number: number
    view_type: string
    trimmed: boolean
    start_time?: number
    end_time?: number
  }
  error?: string
}

// Video Upload API
export const uploadApi = {
  previewVideo: async (videoUrl: string, meetingId: string) => {
    const { data } = await api.post('/upload/preview', {
      video_url: videoUrl,
      meeting_id: meetingId,
    })
    return data as VideoPreviewResponse
  },

  getAutoTrimTimes: async (sessionCode: string, meetingDate: string, videoDurationSeconds: number) => {
    const { data } = await api.post('/upload/auto-trim', {
      session_code: sessionCode,
      meeting_date: meetingDate,
      video_duration_seconds: videoDurationSeconds,
    })
    return data as AutoTrimResponse
  },

  startUpload: async (
    meetingId: string,
    recordingTitle: string,
    sessionCode: string,
    meetingDate: string,
    videoUrl: string,
    viewType: 'gallery' | 'speaker',
    startTime?: number,
    endTime?: number,
    dayNumber?: number
  ) => {
    const { data } = await api.post('/upload/start', {
      meeting_id: meetingId,
      recording_title: recordingTitle,
      session_code: sessionCode,
      meeting_date: meetingDate,
      video_url: videoUrl,
      view_type: viewType,
      start_time: startTime,
      end_time: endTime,
      day_number: dayNumber,
    })
    return data as { success: boolean; job_id: string; message: string }
  },

  getJobStatus: async (jobId: string) => {
    const { data } = await api.get(`/upload/status/${jobId}`)
    return data as UploadJobStatus
  },

  getDayNumber: async (sessionCode: string, meetingDate: string) => {
    const { data } = await api.get(`/upload/day-number/${sessionCode}/${encodeURIComponent(meetingDate)}`)
    return data as { session_code: string; meeting_date: string; day_number: number; found: boolean }
  },

  listJobs: async () => {
    const { data } = await api.get('/upload/jobs')
    return data as {
      jobs: Array<{
        job_id: string
        status: string
        progress: number
        message: string
        session_code: string
        view_type: string
        meeting_date: string
      }>
      total: number
    }
  },
}

// ============================================================================
// Live Tutor
// ============================================================================

export interface TutorCapabilities {
  reminders: boolean
  answer_questions: boolean
  direct_messages: boolean
  summon_dismiss: boolean
}

export interface TutorGuardrails {
  min_seconds_between_messages: number
  max_ai_messages_per_session: number
  quiet_mode: boolean
}

export interface TutorBotConfig {
  display_name: string
  announce_on_join: boolean
  announcement: string
}

export interface TutorCaptureConfig {
  enabled: boolean
  interval_seconds: number
  store_images: boolean
}

export interface TutorSettings {
  capabilities: TutorCapabilities
  autonomy: string
  guardrails: TutorGuardrails
  bot: TutorBotConfig
  capture: TutorCaptureConfig
}

export interface TutorStatus {
  success: boolean
  bot_configured: boolean
  responder_available: boolean
  pending_approvals: number
  active_sessions: number
  settings: TutorSettings
}

export interface TutorReminder {
  id: number
  label: string
  message: string
  enabled: number
  created_at: number
  updated_at: number
}

export interface TutorPolicy {
  id: number
  title: string
  content: string
  enabled: number
  created_at: number
  updated_at: number
}

export interface TutorSession {
  id: number
  meeting_id: string
  meeting_uuid: string | null
  topic: string | null
  session_code: string | null
  status: string
  runtime_id: string | null
  join_url: string | null
  overrides: Record<string, unknown> | null
  summoned_by: string | null
  error: string | null
  created_at: number
  updated_at: number
}

export interface TutorApproval {
  id: number
  session_id: number | null
  meeting_id: string | null
  channel: 'public' | 'dm'
  target_id: string | null
  target_name: string | null
  source: string
  reason: string | null
  draft_text: string
  final_text: string | null
  context: Record<string, unknown> | null
  confidence: string | null
  status: 'pending' | 'approved' | 'rejected' | 'sent' | 'failed'
  decided_by: string | null
  decided_at: number | null
  created_at: number
  updated_at: number
}

export interface TutorMessage {
  id: number
  session_id: number | null
  meeting_id: string | null
  direction: 'inbound' | 'outbound'
  channel: 'public' | 'dm'
  participant_id: string | null
  participant_name: string | null
  text: string
  source: string
  reason: string | null
  approval_id: number | null
  created_at: number
}

export interface TutorScreenshot {
  id: number
  session_id: number | null
  meeting_id: string | null
  participant_id: string | null
  participant_name: string | null
  registrant_id: string | null
  captured_at: number
  video_on: number
  face_present: number
  stored: number
  image_url: string | null
  drive_file_id: string | null
  created_at: number
}

export const tutorApi = {
  getStatus: async () => {
    const { data } = await api.get('/tutor/status')
    return data as TutorStatus
  },
  getSettings: async () => {
    const { data } = await api.get('/tutor/settings')
    return data.settings as TutorSettings
  },
  patchSettings: async (patch: Partial<TutorSettings>) => {
    const { data } = await api.patch('/tutor/settings', patch)
    return data.settings as TutorSettings
  },

  listReminders: async () => {
    const { data } = await api.get('/tutor/reminders')
    return data.reminders as TutorReminder[]
  },
  createReminder: async (body: { label: string; message: string; enabled?: boolean }) => {
    const { data } = await api.post('/tutor/reminders', body)
    return data.reminder as TutorReminder
  },
  updateReminder: async (id: number, patch: Partial<{ label: string; message: string; enabled: boolean }>) => {
    const { data } = await api.patch(`/tutor/reminders/${id}`, patch)
    return data.reminder as TutorReminder
  },
  deleteReminder: async (id: number) => {
    await api.delete(`/tutor/reminders/${id}`)
  },

  listPolicies: async () => {
    const { data } = await api.get('/tutor/policies')
    return data.policies as TutorPolicy[]
  },
  createPolicy: async (body: { title: string; content: string; enabled?: boolean }) => {
    const { data } = await api.post('/tutor/policies', body)
    return data.policy as TutorPolicy
  },
  updatePolicy: async (id: number, patch: Partial<{ title: string; content: string; enabled: boolean }>) => {
    const { data } = await api.patch(`/tutor/policies/${id}`, patch)
    return data.policy as TutorPolicy
  },
  deletePolicy: async (id: number) => {
    await api.delete(`/tutor/policies/${id}`)
  },

  listSessions: async () => {
    const { data } = await api.get('/tutor/sessions')
    return data.sessions as TutorSession[]
  },
  summon: async (body: {
    meeting_id: string
    topic?: string
    session_code?: string
    meeting_uuid?: string
    join_url?: string
  }) => {
    const { data } = await api.post('/tutor/sessions/summon', body)
    return data.session as TutorSession
  },
  dismiss: async (sessionId: number) => {
    const { data } = await api.post(`/tutor/sessions/${sessionId}/dismiss`)
    return data.session as TutorSession
  },
  postReminder: async (sessionId: number, body: { reminder_id?: number; text?: string }) => {
    const { data } = await api.post(`/tutor/sessions/${sessionId}/reminder`, body)
    return data.message as TutorMessage
  },
  sendMessage: async (
    sessionId: number,
    body: { channel: 'public' | 'dm'; text: string; target_id?: string; target_name?: string }
  ) => {
    const { data } = await api.post(`/tutor/sessions/${sessionId}/message`, body)
    return data.message as TutorMessage
  },
  requestAiDm: async (
    sessionId: number,
    body: { target_id: string; target_name?: string; instruction: string }
  ) => {
    const { data } = await api.post(`/tutor/sessions/${sessionId}/ai-dm`, body)
    return data.approval as TutorApproval
  },
  simulateInbound: async (
    sessionId: number,
    body: { text: string; channel?: 'public' | 'dm'; participant_name?: string; participant_id?: string }
  ) => {
    const { data } = await api.post(`/tutor/sessions/${sessionId}/simulate-inbound`, body)
    return data as { success: boolean; drafted: boolean; approval: TutorApproval | null }
  },

  listApprovals: async (status?: string) => {
    const { data } = await api.get('/tutor/approvals', { params: status ? { status } : undefined })
    return data.approvals as TutorApproval[]
  },
  approve: async (id: number, finalText?: string) => {
    const { data } = await api.post(`/tutor/approvals/${id}/approve`, { final_text: finalText })
    return data.approval as TutorApproval
  },
  reject: async (id: number) => {
    const { data } = await api.post(`/tutor/approvals/${id}/reject`)
    return data.approval as TutorApproval
  },

  listMessages: async (params?: { session_id?: number; meeting_id?: string; channel?: string; limit?: number }) => {
    const { data } = await api.get('/tutor/messages', { params })
    return data.messages as TutorMessage[]
  },

  listScreenshots: async (params?: { session_id?: number; meeting_id?: string; participant_id?: string; limit?: number }) => {
    const { data } = await api.get('/tutor/screenshots', { params })
    return data.screenshots as TutorScreenshot[]
  },
}

export default api
