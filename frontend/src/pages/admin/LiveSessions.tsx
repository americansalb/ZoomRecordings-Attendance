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

interface ScheduledSession {
  meeting_id: string
  topic: string
  session_code: string | null
  start_time: string
  duration_minutes: number
  host_name: string
  status: 'waiting' | 'started' | 'finished'
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
  getSessions: async (): Promise<{ sessions: LiveSession[]; total: number; timestamp: string }> => {
    const response = await fetch('/api/live/sessions')
    if (!response.ok) throw new Error('Failed to fetch sessions')
    return response.json()
  },

  getScheduled: async (): Promise<{ sessions: ScheduledSession[]; total: number }> => {
    const response = await fetch('/api/live/scheduled?days=7')
    if (!response.ok) throw new Error('Failed to fetch scheduled sessions')
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

  checkAlerts: async (): Promise<{ alerts: unknown[]; total: number }> => {
    const response = await fetch('/api/live/check-alerts')
    if (!response.ok) throw new Error('Failed to check alerts')
    return response.json()
  }
}

export default function LiveSessionsPage() {
  const [selectedSession, setSelectedSession] = useState<LiveSession | null>(null)
  const [activeTab, setActiveTab] = useState<'live' | 'scheduled'>('live')
  const [testAlertStatus, setTestAlertStatus] = useState<string | null>(null)

  // Auto-refresh every 30 seconds for live sessions
  const { data: sessionsData, isLoading, refetch } = useQuery({
    queryKey: ['live-sessions'],
    queryFn: liveApi.getSessions,
    refetchInterval: 30000,
  })

  // Fetch scheduled sessions
  const { data: scheduledData, isLoading: scheduledLoading } = useQuery({
    queryKey: ['scheduled-sessions'],
    queryFn: liveApi.getScheduled,
    refetchInterval: 60000,
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
    } catch {
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
  const scheduledSessions = scheduledData?.sessions || []
  const config = configData?.config

  const formatTime = (isoString: string) => {
    return new Date(isoString).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }

  const formatDate = (isoString: string) => {
    return new Date(isoString).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' })
  }

  const formatDateTime = (isoString: string) => {
    return new Date(isoString).toLocaleString()
  }

  // Group scheduled sessions by date
  const groupedScheduled = scheduledSessions.reduce((acc, session) => {
    const date = formatDate(session.start_time)
    if (!acc[date]) acc[date] = []
    acc[date].push(session)
    return acc
  }, {} as Record<string, ScheduledSession[]>)

  const getStatusBadge = (status: string) => {
    switch (status) {
      case 'started':
        return <span className="px-2 py-1 text-xs font-medium bg-green-100 text-green-800 rounded animate-pulse">In Progress</span>
      case 'waiting':
        return <span className="px-2 py-1 text-xs font-medium bg-yellow-100 text-yellow-800 rounded">Upcoming</span>
      case 'finished':
        return <span className="px-2 py-1 text-xs font-medium bg-gray-100 text-gray-600 rounded">Finished</span>
      default:
        return null
    }
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex justify-between items-start">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Live Sessions Monitor</h1>
          <p className="mt-1 text-gray-600">Real-time view of active Zoom sessions and scheduled meetings</p>
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
          <div className="flex items-center justify-between flex-wrap gap-2">
            <div className="flex items-center gap-4 flex-wrap">
              <h3 className="font-medium text-gray-900">Alert Configuration</h3>
              <div className="flex gap-3 text-sm flex-wrap">
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

      {/* Tabs */}
      <div className="border-b border-gray-200">
        <nav className="-mb-px flex space-x-8">
          <button
            onClick={() => setActiveTab('live')}
            className={`py-3 px-1 border-b-2 font-medium text-sm transition-colors ${
              activeTab === 'live'
                ? 'border-green-500 text-green-600'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
            }`}
          >
            <span className="flex items-center gap-2">
              <span className={`w-2 h-2 rounded-full ${sessions.length > 0 ? 'bg-green-500 animate-pulse' : 'bg-gray-400'}`}></span>
              Live Sessions ({sessions.length})
            </span>
          </button>
          <button
            onClick={() => setActiveTab('scheduled')}
            className={`py-3 px-1 border-b-2 font-medium text-sm transition-colors ${
              activeTab === 'scheduled'
                ? 'border-blue-500 text-blue-600'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
            }`}
          >
            Scheduled ({scheduledSessions.length})
          </button>
        </nav>
      </div>

      {/* Live Sessions Tab */}
      {activeTab === 'live' && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Sessions List */}
          <div className="bg-white rounded-lg shadow">
            <div className="p-4 border-b">
              <h2 className="text-lg font-semibold text-gray-900">Active Sessions</h2>
            </div>

            {isLoading ? (
              <div className="p-8 text-center text-gray-500">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600 mx-auto mb-4"></div>
                Loading sessions...
              </div>
            ) : sessions.length === 0 ? (
              <div className="p-8 text-center text-gray-500">
                <div className="text-4xl mb-2">📭</div>
                <p className="font-medium">No active sessions</p>
                <p className="text-sm mt-1">Sessions will appear here when someone joins a Zoom meeting</p>
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
                        Started: {formatTime(session.start_time)}
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
                    <p className="text-sm text-gray-500">No participant details available (requires Business plan API)</p>
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
                  Started: {formatDateTime(selectedSession.start_time)}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Scheduled Sessions Tab (Calendar View) */}
      {activeTab === 'scheduled' && (
        <div className="space-y-6">
          {scheduledLoading ? (
            <div className="bg-white rounded-lg shadow p-8 text-center">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600 mx-auto mb-4"></div>
              <p className="text-gray-600">Loading scheduled sessions...</p>
            </div>
          ) : scheduledSessions.length === 0 ? (
            <div className="bg-white rounded-lg shadow p-8 text-center">
              <div className="text-gray-400 text-5xl mb-4">📅</div>
              <h3 className="text-lg font-medium text-gray-900 mb-2">No Scheduled Sessions</h3>
              <p className="text-gray-600">No upcoming Zoom meetings found in the next 7 days.</p>
            </div>
          ) : (
            Object.entries(groupedScheduled).map(([date, daySessions]) => (
              <div key={date} className="bg-white rounded-lg shadow overflow-hidden">
                <div className="bg-gray-50 px-4 py-3 border-b border-gray-200">
                  <h3 className="font-semibold text-gray-900">{date}</h3>
                </div>
                <div className="divide-y divide-gray-200">
                  {daySessions.map((session) => (
                    <div key={session.meeting_id} className="p-4 hover:bg-gray-50">
                      <div className="flex justify-between items-center">
                        <div className="flex-1">
                          <div className="flex items-center gap-3 mb-1">
                            <span className="text-lg font-medium text-gray-900">
                              {formatTime(session.start_time)}
                            </span>
                            <span className="text-gray-400">•</span>
                            <span className="text-gray-600">{session.duration_minutes} min</span>
                            {getStatusBadge(session.status)}
                          </div>
                          <div className="text-gray-900 font-medium">{session.topic}</div>
                          <div className="text-sm text-gray-600">Host: {session.host_name}</div>
                        </div>
                        {session.session_code && (
                          <span className="px-3 py-1 bg-blue-100 text-blue-800 text-sm font-medium rounded">
                            Session {session.session_code}
                          </span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))
          )}
        </div>
      )}

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

      {/* Auto-refresh indicator */}
      <div className="text-center text-sm text-gray-500">
        Auto-refreshing every 30 seconds • Last updated: {sessionsData?.timestamp ? formatDateTime(sessionsData.timestamp) : 'Never'}
      </div>
    </div>
  )
}
