import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { recordingsApi, attendanceApi, Recording, Participant } from '../../services/api'

export default function RecordingsPage() {
  const queryClient = useQueryClient()

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
  } | null>(null)
  const [isProcessing, setIsProcessing] = useState(false)
  const [processResult, setProcessResult] = useState<any>(null)

  const { data: recordingsData, isLoading } = useQuery({
    queryKey: ['recordings', searchTerm],
    queryFn: () => recordingsApi.list({ search: searchTerm || undefined }),
  })

  const previewMutation = useMutation({
    mutationFn: async (recording: Recording) => {
      // Use recording.id (UUID) not meeting_id for recurring meetings
      return attendanceApi.preview(recording.id, recording.topic)
    },
    onSuccess: (data) => {
      setPreviewData(data)
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
        <p className="mt-1 text-gray-600">Select a recording to process attendance</p>
      </div>

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

              {/* Scheduled Start Time Input (Optional - auto-detected from Zoom) */}
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Scheduled Start Time <span className="text-gray-400 font-normal">(optional)</span>
                </label>
                <input
                  type="time"
                  className="input"
                  value={scheduledStartTime}
                  onChange={(e) => setScheduledStartTime(e.target.value)}
                  placeholder="Auto-detected"
                />
                <p className="text-xs text-gray-500 mt-1">
                  Leave empty to use Zoom's scheduled time. Only override if auto-detection fails.
                </p>
              </div>

              {/* Meeting Duration Input (Optional - auto-detected from Zoom) */}
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Scheduled Duration <span className="text-gray-400 font-normal">(optional)</span>
                </label>
                <input
                  type="number"
                  className="input"
                  value={meetingDurationMinutes ?? ''}
                  onChange={(e) => setMeetingDurationMinutes(e.target.value ? parseInt(e.target.value) : undefined)}
                  placeholder="Auto-detected from Zoom"
                />
                <p className="text-xs text-gray-500 mt-1">
                  Leave empty to use Zoom's scheduled duration. Only override if needed.
                </p>
              </div>

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
