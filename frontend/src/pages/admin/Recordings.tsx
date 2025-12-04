import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { accountsApi, recordingsApi, attendanceApi, Recording, Participant } from '../../services/api'

export default function RecordingsPage() {
  const queryClient = useQueryClient()

  const [selectedUserId, setSelectedUserId] = useState<string | null>(null)
  const [selectedRecording, setSelectedRecording] = useState<Recording | null>(null)
  const [meetingDate, setMeetingDate] = useState(
    new Date().toLocaleDateString('en-US', { month: '2-digit', day: '2-digit' })
  )
  const [meetingDurationMinutes, setMeetingDurationMinutes] = useState<number | undefined>(undefined)
  const [scheduledStartTime, setScheduledStartTime] = useState<string>('') // Empty = use Zoom's scheduled time
  const [searchTerm, setSearchTerm] = useState('')
  const [previewData, setPreviewData] = useState<{
    session_code: string | null
    existing_tab: { name: string; sheet_id: number } | null
    participants: Participant[]
    new_count: number
    existing_count: number
    detected_start_time: string | null
    detected_duration: number | null
    detection_source: string | null
  } | null>(null)
  const [isProcessing, setIsProcessing] = useState(false)
  const [processResult, setProcessResult] = useState<any>(null)

  // Fetch users from Zoom account
  const { data: usersData } = useQuery({
    queryKey: ['zoom-users'],
    queryFn: () => accountsApi.listUsers(),
  })

  // Default to "All Users" (null) when page loads
  // User can select specific users via tabs

  const { data: recordingsData, isLoading } = useQuery({
    queryKey: ['recordings', searchTerm, selectedUserId],
    queryFn: () => recordingsApi.list({
      search: searchTerm || undefined,
      user_id: selectedUserId || undefined
    }),
    // Always fetch - when selectedUserId is null, it fetches all users' recordings
  })

  const previewMutation = useMutation({
    mutationFn: async (recording: Recording) => {
      // Use recording.id (UUID) not meeting_id for recurring meetings
      return attendanceApi.preview(recording.id, recording.topic)
    },
    onSuccess: (data) => {
      setPreviewData(data)

      // Pre-fill detected duration
      if (data.detected_duration) {
        setMeetingDurationMinutes(data.detected_duration)
      }

      // Pre-fill detected start time (convert from ISO to local HH:MM)
      if (data.detected_start_time) {
        const detectedDate = new Date(data.detected_start_time)
        const hours = String(detectedDate.getHours()).padStart(2, '0')
        const minutes = String(detectedDate.getMinutes()).padStart(2, '0')
        setScheduledStartTime(`${hours}:${minutes}`)
      }
    },
  })

  const processMutation = useMutation({
    mutationFn: async () => {
      if (!selectedRecording) return

      // Build ISO timestamp from date + scheduled start time, convert to UTC
      // meetingDate is MM/DD, scheduledStartTime is HH:MM (local time)
      let startTimeISO: string | undefined
      if (scheduledStartTime && meetingDate) {
        const [month, day] = meetingDate.split('/')
        const year = new Date().getFullYear()
        // Create local date, then convert to ISO (which gives UTC)
        const localDate = new Date(`${year}-${month.padStart(2, '0')}-${day.padStart(2, '0')}T${scheduledStartTime}:00`)
        startTimeISO = localDate.toISOString() // Converts to UTC with Z suffix
      }

      // Use recording.id (UUID) not meeting_id for recurring meetings
      return attendanceApi.process(
        selectedRecording.id,
        selectedRecording.topic,
        meetingDate,
        meetingDurationMinutes,
        startTimeISO
      )
    },
    onSuccess: (data) => {
      setProcessResult(data)
      queryClient.invalidateQueries({ queryKey: ['sheets'] })
    },
  })

  const handleSelectRecording = async (recording: Recording) => {
    setSelectedRecording(recording)
    setPreviewData(null)
    setProcessResult(null)

    // Extract date from recording start time for default
    const recordingDate = new Date(recording.start_time)
    setMeetingDate(
      recordingDate.toLocaleDateString('en-US', { month: '2-digit', day: '2-digit' })
    )

    // Auto-preview
    previewMutation.mutate(recording)
  }

  const handleProcess = async () => {
    setIsProcessing(true)
    try {
      await processMutation.mutateAsync()
    } finally {
      setIsProcessing(false)
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Zoom Recordings</h1>
        <p className="mt-1 text-gray-600">Select a user and recording to process attendance</p>
      </div>

      {/* User Tabs */}
      {usersData && usersData.users.length > 0 && (
        <div className="bg-white rounded-lg shadow-sm p-2 mb-4">
          <div className="flex items-center gap-2 mb-2">
            <svg className="w-5 h-5 text-gray-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
            </svg>
            <h2 className="text-sm font-semibold text-gray-700">Filter by User</h2>
          </div>
          <nav className="flex space-x-2 overflow-x-auto pb-2" aria-label="User Filter">
            <button
              onClick={() => {
                setSelectedUserId(null)
                setSelectedRecording(null)
                setPreviewData(null)
                setProcessResult(null)
              }}
              className={`
                whitespace-nowrap px-4 py-2 rounded-lg font-medium text-sm transition-all
                ${
                  selectedUserId === null
                    ? 'bg-blue-600 text-white shadow-md transform scale-105'
                    : 'bg-gray-100 text-gray-700 hover:bg-gray-200 hover:shadow'
                }
              `}
            >
              <span className="flex items-center gap-2">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
                </svg>
                All Users
              </span>
            </button>
            {usersData.users.map((user) => (
              <button
                key={user.id}
                onClick={() => {
                  setSelectedUserId(user.id)
                  setSelectedRecording(null)
                  setPreviewData(null)
                  setProcessResult(null)
                }}
                className={`
                  whitespace-nowrap px-4 py-2 rounded-lg font-medium text-sm transition-all
                  ${
                    selectedUserId === user.id
                      ? 'bg-blue-600 text-white shadow-md transform scale-105'
                      : 'bg-gray-100 text-gray-700 hover:bg-gray-200 hover:shadow'
                  }
                `}
              >
                <span className="flex items-center gap-2">
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold ${
                    selectedUserId === user.id ? 'bg-blue-500' : 'bg-gray-300 text-gray-700'
                  }`}>
                    {user.email?.[0]?.toUpperCase()}
                  </div>
                  {user.email}
                </span>
              </button>
            ))}
          </nav>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Recordings List */}
        <div className="card">
          <div className="mb-4">
            <input
              type="text"
              placeholder="Search recordings..."
              className="input"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
            />
          </div>

          {isLoading ? (
            <div className="text-center py-8 text-gray-500">Loading recordings...</div>
          ) : (recordingsData?.recordings?.length ?? 0) === 0 ? (
            <div className="text-center py-8 text-gray-500">No recordings found</div>
          ) : (
            <div className="space-y-2 max-h-[600px] overflow-y-auto">
              {(recordingsData?.recordings ?? []).map((recording) => (
                <div
                  key={recording.id}
                  onClick={() => handleSelectRecording(recording)}
                  className={`p-4 rounded-lg cursor-pointer transition-colors ${
                    selectedRecording?.id === recording.id
                      ? 'bg-blue-50 border-2 border-blue-500'
                      : 'bg-gray-50 hover:bg-gray-100 border-2 border-transparent'
                  }`}
                >
                  <div className="flex justify-between items-start">
                    <div className="flex-1 min-w-0">
                      <p className="font-medium text-gray-900 truncate">{recording.topic}</p>
                      <p className="text-sm text-gray-500">
                        {new Date(recording.start_time).toLocaleString()}
                      </p>
                      <p className="text-sm text-gray-500">
                        Duration: {recording.duration} min | Host: {recording.host_name}
                      </p>
                    </div>
                    {recording.session_code && (
                      <span className="ml-2 px-2 py-1 bg-blue-100 text-blue-800 text-xs font-medium rounded flex-shrink-0">
                        {recording.session_code}
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Preview & Process Panel */}
        <div className="card">
          {!selectedRecording ? (
            <div className="text-center py-16 text-gray-500">
              Select a recording to preview attendance
            </div>
          ) : (
            <div className="space-y-4">
              <div>
                <h2 className="text-lg font-semibold text-gray-900">
                  {selectedRecording.topic}
                </h2>
                <p className="text-sm text-gray-500">
                  {new Date(selectedRecording.start_time).toLocaleString()}
                </p>
              </div>

              {/* Meeting Date Input */}
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Meeting Date (MM/DD)
                </label>
                <input
                  type="text"
                  className="input"
                  value={meetingDate}
                  onChange={(e) => setMeetingDate(e.target.value)}
                  placeholder="MM/DD"
                />
              </div>

              {/* Scheduled Start Time Input (Auto-filled, editable) */}
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Scheduled Start Time
                  {previewData?.detected_start_time && (
                    <span className="text-green-600 font-normal text-xs ml-1">✓ Auto-filled</span>
                  )}
                </label>
                <input
                  type="time"
                  className="input"
                  value={scheduledStartTime}
                  onChange={(e) => setScheduledStartTime(e.target.value)}
                  placeholder="HH:MM"
                />
                <p className="text-xs text-gray-500 mt-1">
                  Your local time. Edit to override auto-detected value.
                </p>
              </div>

              {/* Meeting Duration Input (Auto-filled, editable) */}
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Scheduled Duration (minutes)
                  {previewData?.detected_duration && (
                    <span className="text-green-600 font-normal text-xs ml-1">✓ Auto-filled</span>
                  )}
                </label>
                <input
                  type="number"
                  className="input"
                  value={meetingDurationMinutes ?? ''}
                  onChange={(e) => setMeetingDurationMinutes(e.target.value ? parseInt(e.target.value) : undefined)}
                  placeholder="e.g., 180"
                />
                <p className="text-xs text-gray-500 mt-1">
                  Edit to override auto-detected duration.
                </p>
              </div>

              {/* Detected Time Info */}
              {previewData && (previewData.detected_start_time || previewData.detected_duration) && (
                <div className="p-3 bg-blue-50 border border-blue-200 rounded-lg">
                  <div className="flex items-center gap-2 mb-2">
                    <svg className="w-4 h-4 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    <h3 className="text-sm font-semibold text-blue-900">Auto-Detected Schedule</h3>
                    {previewData.detection_source && (
                      <span className="text-xs text-blue-600">({previewData.detection_source})</span>
                    )}
                  </div>
                  {previewData.detected_start_time && (
                    <p className="text-sm text-blue-800 mb-1">
                      <strong>Start Time:</strong>{' '}
                      {new Date(previewData.detected_start_time).toLocaleString('en-US', {
                        hour: '2-digit',
                        minute: '2-digit',
                        hour12: true,
                        timeZoneName: 'short'
                      })}
                      {' '}
                      <span className="text-xs text-blue-600">
                        ({Intl.DateTimeFormat().resolvedOptions().timeZone})
                      </span>
                    </p>
                  )}
                  {previewData.detected_duration && (
                    <p className="text-sm text-blue-800">
                      <strong>Duration:</strong> {previewData.detected_duration} minutes
                      {' '}
                      <span className="text-xs text-blue-600">
                        (with ±5 min grace period)
                      </span>
                    </p>
                  )}
                  <p className="text-xs text-blue-700 mt-2">
                    💡 Values above have been pre-filled. You can edit them if needed.
                  </p>
                </div>
              )}

              {/* Session Info */}
              {previewData && (
                <div className="p-3 bg-gray-50 rounded-lg">
                  <p className="text-sm">
                    <strong>Session:</strong>{' '}
                    {previewData.session_code ? `Session ${previewData.session_code}` : 'No session code found'}
                  </p>
                  <p className="text-sm">
                    <strong>Tab:</strong>{' '}
                    {previewData.existing_tab ? previewData.existing_tab.name : 'Will create new tab'}
                  </p>
                </div>
              )}

              {/* Preview Loading */}
              {previewMutation.isPending && (
                <div className="text-center py-4 text-gray-500">
                  Loading participant data...
                </div>
              )}

              {/* Preview Error */}
              {previewMutation.isError && (
                <div className="p-3 bg-red-50 text-red-700 rounded-lg">
                  Error loading preview: {(previewMutation.error as Error).message}
                </div>
              )}

              {/* Participants Preview */}
              {previewData && (
                <div>
                  <div className="flex justify-between items-center mb-2">
                    <h3 className="font-medium text-gray-900">
                      Participants ({previewData.participants.length})
                    </h3>
                    <div className="text-sm">
                      <span className="text-green-600">{previewData.new_count} new</span>
                      {' | '}
                      <span className="text-blue-600">{previewData.existing_count} existing</span>
                    </div>
                  </div>

                  <div className="max-h-64 overflow-y-auto border rounded-lg">
                    <table className="min-w-full divide-y divide-gray-200">
                      <thead className="bg-gray-50 sticky top-0">
                        <tr>
                          <th className="px-3 py-2 text-left text-xs font-medium text-gray-500">Name</th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-gray-500">Minutes</th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-gray-500">Status</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-200">
                        {previewData.participants.map((p, idx) => (
                          <tr key={idx} className={p.is_new ? 'bg-green-50' : ''}>
                            <td className="px-3 py-2 text-sm">
                              <div>{p.name}</div>
                              {p.email && (
                                <div className="text-xs text-gray-500">{p.email}</div>
                              )}
                            </td>
                            <td className="px-3 py-2 text-sm">{p.attendance_minutes}</td>
                            <td className="px-3 py-2 text-sm">
                              {p.is_new ? (
                                <span className="text-green-600">New</span>
                              ) : (
                                <span className="text-blue-600">Existing</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {/* Process Button */}
              {previewData && !processResult && (
                <button
                  onClick={handleProcess}
                  disabled={isProcessing || !previewData.session_code}
                  className="w-full btn btn-primary disabled:opacity-50"
                >
                  {isProcessing ? 'Processing...' : 'Process Attendance'}
                </button>
              )}

              {!previewData?.session_code && previewData && (
                <p className="text-sm text-red-600">
                  Cannot process: No session code found in recording title.
                  Expected format: "Session XXX..."
                </p>
              )}

              {/* Process Result */}
              {processResult && (
                <div className="p-4 bg-green-50 rounded-lg">
                  <h3 className="font-medium text-green-800 mb-2">Attendance Processed!</h3>
                  <p className="text-sm text-green-700">
                    Session: {processResult.session_code}
                  </p>
                  <p className="text-sm text-green-700">
                    Date: {processResult.meeting_date}
                  </p>
                  <p className="text-sm text-green-700">
                    New profiles: {processResult.results.new_profiles}
                  </p>
                  <p className="text-sm text-green-700">
                    Updated profiles: {processResult.results.updated_profiles}
                  </p>
                  <a
                    href={`https://docs.google.com/spreadsheets/d/${processResult.spreadsheet_id}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-block mt-2 text-blue-600 hover:text-blue-800"
                  >
                    Open Google Sheet
                  </a>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
