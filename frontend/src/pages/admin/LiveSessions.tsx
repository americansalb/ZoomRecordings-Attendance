import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

interface Participant {
  name: string
  email: string | null
  role: 'host' | 'co-host' | 'student'
  is_trainer: boolean
  join_time: string
}

interface LiveSession {
  meeting_id: string
  topic: string
  session_code: string | null
  start_time: string
  host_name: string
  trainer_count: number
  student_count: number
  has_trainer: boolean
  participants: Participant[]
}

interface LiveStats {
  active_sessions: number
  sessions_with_trainers: number
  sessions_without_trainers: number
  total_trainers: number
  total_students: number
}

interface NotificationConfig {
  smtp_configured: boolean
  alert_recipients_count: number
  google_spaces_configured: boolean
}

// API functions
const liveApi = {
  getSessions: async (): Promise<{ sessions: LiveSession[]; total: number }> => {
    const response = await fetch('/api/live/sessions')
    if (!response.ok) throw new Error('Failed to fetch sessions')
    return response.json()
  },

  getStats: async (): Promise<{ stats: LiveStats }> => {
    const response = await fetch('/api/live/stats')
    if (!response.ok) throw new Error('Failed to fetch stats')
    return response.json()
  },

  getConfig: async (): Promise<{ config: NotificationConfig }> => {
    const response = await fetch('/api/live/config')
    if (!response.ok) throw new Error('Failed to fetch config')
    return response.json()
  },

  sendTestAlert: async (): Promise<{ success: boolean; message: string }> => {
    const response = await fetch('/api/live/test-alert', { method: 'POST' })
    if (!response.ok) throw new Error('Failed to send test alert')
    return response.json()
  },

  checkAlerts: async (): Promise<{ alerts: any[]; total: number }> => {
    const response = await fetch('/api/live/check-alerts')
    if (!response.ok) throw new Error('Failed to check alerts')
    return response.json()
  }
}

export default function LiveSessionsPage() {
  const [selectedSession, setSelectedSession] = useState<LiveSession | null>(null)
  const [testAlertStatus, setTestAlertStatus] = useState<string | null>(null)

  // Auto-refresh every 30 seconds
  const { data: sessionsData, isLoading, refetch } = useQuery({
    queryKey: ['live-sessions'],
    queryFn: liveApi.getSessions,
    refetchInterval: 30000, // Refresh every 30 seconds
  })

  const { data: statsData } = useQuery({
    queryKey: ['live-stats'],
    queryFn: liveApi.getStats,
    refetchInterval: 30000,
  })

  const { data: configData } = useQuery({
    queryKey: ['live-config'],
    queryFn: liveApi.getConfig,
  })

  const handleTestAlert = async () => {
    try {
      setTestAlertStatus('Sending...')
      const result = await liveApi.sendTestAlert()
      setTestAlertStatus(result.message)
      setTimeout(() => setTestAlertStatus(null), 5000)
    } catch (error) {
      setTestAlertStatus('Failed to send test alert')
      setTimeout(() => setTestAlertStatus(null), 5000)
    }
  }

  const handleManualCheck = async () => {
    try {
      await liveApi.checkAlerts()
      refetch()
    } catch (error) {
      console.error('Failed to check alerts:', error)
    }
  }

  const stats = statsData?.stats
  const sessions = sessionsData?.sessions || []
  const config = configData?.config

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex justify-between items-start">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Live Sessions Monitor</h1>
          <p className="mt-1 text-gray-600">Real-time view of active Zoom sessions and trainer presence</p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => refetch()}
            className="px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 transition-colors"
          >
            Refresh
          </button>
          <button
            onClick={handleManualCheck}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
          >
            Check Alerts
          </button>
        </div>
      </div>

      {/* Stats Cards */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          <div className="bg-white rounded-lg shadow p-4">
            <p className="text-sm text-gray-500">Active Sessions</p>
            <p className="text-3xl font-bold text-gray-900">{stats.active_sessions}</p>
          </div>
          <div className="bg-white rounded-lg shadow p-4">
            <p className="text-sm text-gray-500">With Trainers</p>
            <p className="text-3xl font-bold text-green-600">{stats.sessions_with_trainers}</p>
          </div>
          <div className="bg-white rounded-lg shadow p-4">
            <p className="text-sm text-gray-500">No Trainer</p>
            <p className={`text-3xl font-bold ${stats.sessions_without_trainers > 0 ? 'text-red-600' : 'text-gray-400'}`}>
              {stats.sessions_without_trainers}
            </p>
          </div>
          <div className="bg-white rounded-lg shadow p-4">
            <p className="text-sm text-gray-500">Total Trainers</p>
            <p className="text-3xl font-bold text-blue-600">{stats.total_trainers}</p>
          </div>
          <div className="bg-white rounded-lg shadow p-4">
            <p className="text-sm text-gray-500">Total Students</p>
            <p className="text-3xl font-bold text-purple-600">{stats.total_students}</p>
          </div>
        </div>
      )}

      {/* Notification Config Status */}
      {config && (
        <div className="bg-white rounded-lg shadow p-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-4">
              <h3 className="font-medium text-gray-900">Alert Configuration</h3>
              <div className="flex gap-3 text-sm">
                <span className={`px-2 py-1 rounded ${config.smtp_configured ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'}`}>
                  Email: {config.smtp_configured ? 'Configured' : 'Not Set'}
                </span>
                <span className="px-2 py-1 rounded bg-gray-100 text-gray-700">
                  Recipients: {config.alert_recipients_count}
                </span>
                <span className={`px-2 py-1 rounded ${config.google_spaces_configured ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-500'}`}>
                  Google Spaces: {config.google_spaces_configured ? 'Configured' : 'Not Set'}
                </span>
              </div>
            </div>
            <div className="flex items-center gap-2">
              {testAlertStatus && (
                <span className="text-sm text-gray-600">{testAlertStatus}</span>
              )}
              <button
                onClick={handleTestAlert}
                disabled={!config.smtp_configured}
                className="px-3 py-1 text-sm bg-yellow-100 text-yellow-800 rounded hover:bg-yellow-200 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Send Test Alert
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Sessions Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Sessions List */}
        <div className="bg-white rounded-lg shadow">
          <div className="p-4 border-b">
            <h2 className="text-lg font-semibold text-gray-900">Active Sessions</h2>
          </div>

          {isLoading ? (
            <div className="p-8 text-center text-gray-500">Loading sessions...</div>
          ) : sessions.length === 0 ? (
            <div className="p-8 text-center text-gray-500">
              <div className="text-4xl mb-2">📭</div>
              <p>No active sessions at the moment</p>
            </div>
          ) : (
            <div className="divide-y max-h-[600px] overflow-y-auto">
              {sessions.map((session) => (
                <div
                  key={session.meeting_id}
                  onClick={() => setSelectedSession(session)}
                  className={`p-4 cursor-pointer hover:bg-gray-50 transition-colors ${
                    selectedSession?.meeting_id === session.meeting_id ? 'bg-blue-50' : ''
                  }`}
                >
                  <div className="flex justify-between items-start mb-2">
                    <div>
                      <h3 className="font-medium text-gray-900 truncate max-w-[300px]">
                        {session.topic}
                      </h3>
                      {session.session_code && (
                        <span className="text-xs bg-blue-100 text-blue-800 px-2 py-0.5 rounded">
                          Session {session.session_code}
                        </span>
                      )}
                    </div>
                    {!session.has_trainer && session.student_count > 0 ? (
                      <span className="px-2 py-1 bg-red-100 text-red-800 text-xs font-medium rounded-full animate-pulse">
                        NO TRAINER
                      </span>
                    ) : session.has_trainer ? (
                      <span className="px-2 py-1 bg-green-100 text-green-800 text-xs font-medium rounded-full">
                        Trainer Present
                      </span>
                    ) : (
                      <span className="px-2 py-1 bg-gray-100 text-gray-600 text-xs font-medium rounded-full">
                        Empty
                      </span>
                    )}
                  </div>

                  <div className="flex gap-4 text-sm text-gray-500">
                    <span>Host: {session.host_name}</span>
                    <span>
                      Started: {new Date(session.start_time).toLocaleTimeString()}
                    </span>
                  </div>

                  <div className="flex gap-4 mt-2 text-sm">
                    <span className="text-blue-600">
                      {session.trainer_count} trainer{session.trainer_count !== 1 ? 's' : ''}
                    </span>
                    <span className="text-purple-600">
                      {session.student_count} student{session.student_count !== 1 ? 's' : ''}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Session Details */}
        <div className="bg-white rounded-lg shadow">
          <div className="p-4 border-b">
            <h2 className="text-lg font-semibold text-gray-900">
              {selectedSession ? 'Session Details' : 'Select a Session'}
            </h2>
          </div>

          {!selectedSession ? (
            <div className="p-8 text-center text-gray-500">
              <div className="text-4xl mb-2">👈</div>
              <p>Click on a session to view participant details</p>
            </div>
          ) : (
            <div className="p-4">
              <div className="mb-4">
                <h3 className="font-medium text-gray-900">{selectedSession.topic}</h3>
                <p className="text-sm text-gray-500">
                  Meeting ID: {selectedSession.meeting_id}
                </p>
              </div>

              <div className="mb-4">
                <h4 className="font-medium text-gray-700 mb-2">
                  Participants ({selectedSession.participants.length})
                </h4>

                {selectedSession.participants.length === 0 ? (
                  <p className="text-sm text-gray-500">No participants yet</p>
                ) : (
                  <div className="space-y-2 max-h-[400px] overflow-y-auto">
                    {/* Trainers first */}
                    {selectedSession.participants
                      .filter(p => p.is_trainer)
                      .map((participant, idx) => (
                        <div
                          key={idx}
                          className="flex items-center justify-between p-3 bg-blue-50 rounded-lg"
                        >
                          <div>
                            <p className="font-medium text-gray-900">{participant.name}</p>
                            {participant.email && (
                              <p className="text-sm text-gray-500">{participant.email}</p>
                            )}
                          </div>
                          <span className={`px-2 py-1 text-xs font-medium rounded ${
                            participant.role === 'host'
                              ? 'bg-blue-600 text-white'
                              : 'bg-blue-200 text-blue-800'
                          }`}>
                            {participant.role === 'host' ? 'HOST' : 'CO-HOST'}
                          </span>
                        </div>
                      ))}

                    {/* Students */}
                    {selectedSession.participants
                      .filter(p => !p.is_trainer)
                      .map((participant, idx) => (
                        <div
                          key={idx}
                          className="flex items-center justify-between p-3 bg-gray-50 rounded-lg"
                        >
                          <div>
                            <p className="font-medium text-gray-900">{participant.name}</p>
                            {participant.email && (
                              <p className="text-sm text-gray-500">{participant.email}</p>
                            )}
                          </div>
                          <span className="px-2 py-1 text-xs font-medium rounded bg-gray-200 text-gray-700">
                            STUDENT
                          </span>
                        </div>
                      ))}
                  </div>
                )}
              </div>

              <div className="text-xs text-gray-400">
                Started: {new Date(selectedSession.start_time).toLocaleString()}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Alert Legend */}
      <div className="bg-white rounded-lg shadow p-4">
        <h3 className="font-medium text-gray-900 mb-3">Alert Schedule</h3>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm">
          <div className="flex items-center gap-2">
            <span className="w-3 h-3 rounded-full bg-yellow-400"></span>
            <span><strong>5 min before:</strong> Warning - Trainer not yet joined</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="w-3 h-3 rounded-full bg-orange-500"></span>
            <span><strong>2 min before:</strong> Urgent - Still no trainer</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="w-3 h-3 rounded-full bg-red-600"></span>
            <span><strong>5 min after:</strong> Critical - Students without supervision</span>
          </div>
        </div>
      </div>
    </div>
  )
}
