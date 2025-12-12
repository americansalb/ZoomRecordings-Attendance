import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { accountsApi, recordingsApi, attendanceApi, proctorApi, Recording, Participant, RecordingFile, ProctorJobStatus, ProctorResult } from '../../services/api'

export default function RecordingsPage() {
  const queryClient = useQueryClient()

  const [selectedUserId, setSelectedUserId] = useState<string | null>(null)
  const [selectedRecording, setSelectedRecording] = useState<Recording | null>(null)
  const [meetingDate, setMeetingDate] = useState(
    new Date().toLocaleDateString('en-US', { month: '2-digit', day: '2-digit' })
  )
  const [meetingDurationMinutes, setMeetingDurationMinutes] = useState<number | undefined>(undefined)
  const [scheduledStartTime, setScheduledStartTime] = useState<string>('') // Empty = use Zoom's scheduled time
  const [numberOfSegments, setNumberOfSegments] = useState<number | undefined>(undefined)
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
    detection_warnings: string[]
  } | null>(null)
  const [isProcessing, setIsProcessing] = useState(false)
  const [processResult, setProcessResult] = useState<any>(null)

  // Proctoring state
  const [activeMode, setActiveMode] = useState<'attendance' | 'proctoring'>('attendance')
  const [selectedVideoFile, setSelectedVideoFile] = useState<RecordingFile | null>(null)
  const [proctorJobId, setProctorJobId] = useState<string | null>(null)
  const [proctorJobStatus, setProctorJobStatus] = useState<ProctorJobStatus | null>(null)
  const [proctorResult, setProctorResult] = useState<ProctorResult | null>(null)
  const [isProctoring, setIsProctoring] = useState(false)
  const [sampleInterval, setSampleInterval] = useState<number>(30)

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
        startTimeISO,
        numberOfSegments
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
    setSelectedVideoFile(null)
    setProctorJobId(null)
    setProctorJobStatus(null)
    setProctorResult(null)

    // Extract date from recording start time for default
    const recordingDate = new Date(recording.start_time)
    setMeetingDate(
      recordingDate.toLocaleDateString('en-US', { month: '2-digit', day: '2-digit' })
    )

    // Auto-preview
    previewMutation.mutate(recording)

    // Auto-select gallery view video if available
    if (recording.recording_files) {
      const galleryView = recording.recording_files.find(
        f => f.recording_type === 'gallery_view' || f.recording_type === 'shared_screen_with_gallery_view'
      )
      if (galleryView) {
        setSelectedVideoFile(galleryView)
      } else {
        // Fallback to first MP4 file
        const mp4File = recording.recording_files.find(f => f.file_type === 'MP4')
        if (mp4File) setSelectedVideoFile(mp4File)
      }
    }
  }

  // Poll for proctor job status
  useEffect(() => {
    if (!proctorJobId || proctorJobStatus?.status === 'completed' || proctorJobStatus?.status === 'failed') {
      return
    }

    const pollInterval = setInterval(async () => {
      try {
        const status = await proctorApi.getJobStatus(proctorJobId)
        setProctorJobStatus(status)

        if (status.status === 'completed' && status.result) {
          setProctorResult(status.result)
          setIsProctoring(false)
        } else if (status.status === 'failed') {
          setIsProctoring(false)
        }
      } catch (error) {
        console.error('Error polling proctor status:', error)
      }
    }, 2000) // Poll every 2 seconds

    return () => clearInterval(pollInterval)
  }, [proctorJobId, proctorJobStatus?.status])

  const handleStartProctoring = async () => {
    if (!selectedRecording || !selectedVideoFile || !previewData?.session_code) return

    setIsProctoring(true)
    setProctorResult(null)
    setProctorJobStatus(null)

    try {
      // Get participant names from preview
      const participantNames = previewData.participants.map(p => p.name)

      const response = await proctorApi.startProcessing(
        selectedRecording.id,
        selectedRecording.topic,
        previewData.session_code,
        meetingDate,
        selectedVideoFile.download_url,
        participantNames,
        undefined, // auto-detect grid
        sampleInterval
      )

      setProctorJobId(response.job_id)
      setProctorJobStatus({
        job_id: response.job_id,
        status: 'pending',
        progress: 0,
        message: response.message
      })
    } catch (error) {
      console.error('Error starting proctoring:', error)
      setIsProctoring(false)
    }
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

              {/* Mode Switcher */}
              <div className="flex border-b border-gray-200">
                <button
                  onClick={() => setActiveMode('attendance')}
                  className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                    activeMode === 'attendance'
                      ? 'border-blue-500 text-blue-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700'
                  }`}
                >
                  Attendance
                </button>
                <button
                  onClick={() => setActiveMode('proctoring')}
                  className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                    activeMode === 'proctoring'
                      ? 'border-purple-500 text-purple-600'
                      : 'border-transparent text-gray-500 hover:text-gray-700'
                  }`}
                >
                  Video Proctoring
                </button>
              </div>

              {/* ATTENDANCE MODE */}
              {activeMode === 'attendance' && (
                <>
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

              {/* Time Segments Input */}
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Time Segments (optional)
                </label>
                <select
                  className="input"
                  value={numberOfSegments ?? ''}
                  onChange={(e) => setNumberOfSegments(e.target.value ? parseInt(e.target.value) : undefined)}
                >
                  <option value="">No segmentation</option>
                  <option value="2">2 segments</option>
                  <option value="3">3 segments (e.g., hourly for 3hr meeting)</option>
                  <option value="4">4 segments</option>
                  <option value="6">6 segments (e.g., 30-min for 3hr meeting)</option>
                </select>
                <p className="text-xs text-gray-500 mt-1">
                  Divide attendance into time segments (shows hour-by-hour breakdown in sheet)
                </p>
              </div>

              {/* Detection Warnings */}
              {previewData && previewData.detection_warnings && previewData.detection_warnings.length > 0 && (
                <div className="p-3 bg-yellow-50 border border-yellow-300 rounded-lg">
                  <div className="flex items-center gap-2 mb-2">
                    <svg className="w-4 h-4 text-yellow-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                    </svg>
                    <h3 className="text-sm font-semibold text-yellow-900">Detection Issues</h3>
                  </div>
                  <ul className="text-sm text-yellow-800 list-disc list-inside space-y-1">
                    {previewData.detection_warnings.map((warning, idx) => (
                      <li key={idx}>{warning}</li>
                    ))}
                  </ul>
                  <p className="text-xs text-yellow-700 mt-2">
                    ⚠️ Please manually enter the scheduled time and duration below.
                  </p>
                </div>
              )}

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
                </>
              )}

              {/* PROCTORING MODE */}
              {activeMode === 'proctoring' && (
                <>
                  {/* Video File Selection */}
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">
                      Select Video File
                    </label>
                    {selectedRecording.recording_files && selectedRecording.recording_files.length > 0 ? (
                      <select
                        className="input"
                        value={selectedVideoFile?.id || ''}
                        onChange={(e) => {
                          const file = selectedRecording.recording_files?.find(f => f.id === e.target.value)
                          setSelectedVideoFile(file || null)
                        }}
                      >
                        <option value="">Select a video file...</option>
                        {selectedRecording.recording_files
                          .filter(f => f.file_type === 'MP4')
                          .map((file) => (
                            <option key={file.id} value={file.id}>
                              {file.recording_type.replace(/_/g, ' ')} ({(file.file_size / 1024 / 1024).toFixed(1)} MB)
                            </option>
                          ))}
                      </select>
                    ) : (
                      <p className="text-sm text-gray-500">No video files available</p>
                    )}
                    <p className="text-xs text-gray-500 mt-1">
                      Select "gallery view" for best face detection results
                    </p>
                  </div>

                  {/* Sample Interval */}
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">
                      Sample Interval (seconds)
                    </label>
                    <select
                      className="input"
                      value={sampleInterval}
                      onChange={(e) => setSampleInterval(parseInt(e.target.value))}
                    >
                      <option value="15">15 seconds (more detailed)</option>
                      <option value="30">30 seconds (recommended)</option>
                      <option value="60">60 seconds (faster)</option>
                    </select>
                    <p className="text-xs text-gray-500 mt-1">
                      How often to check for face visibility
                    </p>
                  </div>

                  {/* Session Info */}
                  {previewData && (
                    <div className="p-3 bg-purple-50 rounded-lg">
                      <p className="text-sm">
                        <strong>Session:</strong>{' '}
                        {previewData.session_code ? `Session ${previewData.session_code}` : 'No session code found'}
                      </p>
                      <p className="text-sm">
                        <strong>Participants:</strong> {previewData.participants.length}
                      </p>
                      <p className="text-sm">
                        <strong>Date:</strong> {meetingDate}
                      </p>
                    </div>
                  )}

                  {/* Start Proctoring Button */}
                  {!proctorResult && !isProctoring && (
                    <button
                      onClick={handleStartProctoring}
                      disabled={!selectedVideoFile || !previewData?.session_code || previewMutation.isPending}
                      className="w-full btn bg-purple-600 hover:bg-purple-700 text-white disabled:opacity-50"
                    >
                      Start Video Proctoring
                    </button>
                  )}

                  {!selectedVideoFile && (
                    <p className="text-sm text-red-600">
                      Please select a video file to proctor
                    </p>
                  )}

                  {/* Proctoring Progress */}
                  {proctorJobStatus && proctorJobStatus.status !== 'completed' && (
                    <div className="p-4 bg-purple-50 rounded-lg">
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-sm font-medium text-purple-900">
                          {proctorJobStatus.message}
                        </span>
                        <span className="text-sm text-purple-600">
                          {Math.round(proctorJobStatus.progress * 100)}%
                        </span>
                      </div>
                      <div className="w-full bg-purple-200 rounded-full h-2">
                        <div
                          className="bg-purple-600 h-2 rounded-full transition-all duration-300"
                          style={{ width: `${proctorJobStatus.progress * 100}%` }}
                        />
                      </div>
                      {proctorJobStatus.status === 'failed' && proctorJobStatus.error && (
                        <p className="text-sm text-red-600 mt-2">
                          Error: {proctorJobStatus.error}
                        </p>
                      )}
                    </div>
                  )}

                  {/* Proctoring Results */}
                  {proctorResult && (
                    <div className="space-y-4">
                      <div className="p-4 bg-green-50 rounded-lg">
                        <h3 className="font-medium text-green-800 mb-2">Proctoring Complete!</h3>
                        <p className="text-sm text-green-700">
                          Duration: {proctorResult.total_duration_minutes.toFixed(0)} minutes
                        </p>
                        <p className="text-sm text-green-700">
                          Frames analyzed: {proctorResult.frames_analyzed}
                        </p>
                      </div>

                      {/* Participant Results */}
                      <div>
                        <h3 className="font-medium text-gray-900 mb-2">
                          Participant Visibility
                        </h3>
                        <div className="max-h-64 overflow-y-auto border rounded-lg">
                          <table className="min-w-full divide-y divide-gray-200">
                            <thead className="bg-gray-50 sticky top-0">
                              <tr>
                                <th className="px-3 py-2 text-left text-xs font-medium text-gray-500">Name</th>
                                <th className="px-3 py-2 text-left text-xs font-medium text-gray-500">Visibility</th>
                                <th className="px-3 py-2 text-left text-xs font-medium text-gray-500">Violations</th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-gray-200">
                              {proctorResult.participants.map((p, idx) => (
                                <tr
                                  key={idx}
                                  className={p.visibility_percentage < 80 ? 'bg-red-50' : p.visibility_percentage < 95 ? 'bg-yellow-50' : ''}
                                >
                                  <td className="px-3 py-2 text-sm">{p.name}</td>
                                  <td className="px-3 py-2 text-sm">
                                    <span className={`font-medium ${
                                      p.visibility_percentage >= 95 ? 'text-green-600' :
                                      p.visibility_percentage >= 80 ? 'text-yellow-600' : 'text-red-600'
                                    }`}>
                                      {p.visibility_percentage.toFixed(1)}%
                                    </span>
                                  </td>
                                  <td className="px-3 py-2 text-sm">
                                    {p.violation_count > 0 ? (
                                      <span className="text-red-600">
                                        {p.violation_count} ({p.total_violation_minutes.toFixed(1)} min)
                                      </span>
                                    ) : (
                                      <span className="text-green-600">None</span>
                                    )}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </div>

                      {/* Summary Stats */}
                      <div className="grid grid-cols-3 gap-2 text-center">
                        <div className="p-2 bg-green-100 rounded">
                          <p className="text-2xl font-bold text-green-700">
                            {proctorResult.participants.filter(p => p.visibility_percentage >= 95).length}
                          </p>
                          <p className="text-xs text-green-600">95%+ visible</p>
                        </div>
                        <div className="p-2 bg-yellow-100 rounded">
                          <p className="text-2xl font-bold text-yellow-700">
                            {proctorResult.participants.filter(p => p.visibility_percentage >= 80 && p.visibility_percentage < 95).length}
                          </p>
                          <p className="text-xs text-yellow-600">80-95% visible</p>
                        </div>
                        <div className="p-2 bg-red-100 rounded">
                          <p className="text-2xl font-bold text-red-700">
                            {proctorResult.participants.filter(p => p.visibility_percentage < 80).length}
                          </p>
                          <p className="text-xs text-red-600">&lt;80% visible</p>
                        </div>
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
