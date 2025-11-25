import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { sheetsApi, recordingsApi } from '../../services/api'

export default function AdminDashboard() {
  const { data: sheetsData, isLoading: sheetsLoading } = useQuery({
    queryKey: ['sheets'],
    queryFn: sheetsApi.list,
  })

  const { data: recordingsData, isLoading: recordingsLoading } = useQuery({
    queryKey: ['recordings'],
    queryFn: () => recordingsApi.list(),
  })

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Admin Dashboard</h1>
        <p className="mt-1 text-gray-600">Manage attendance across all sessions</p>
      </div>

      {/* Quick Actions */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Link
          to="/recordings"
          className="card hover:shadow-lg transition-shadow cursor-pointer"
        >
          <div className="flex items-center">
            <div className="p-3 bg-blue-100 rounded-lg">
              <svg className="w-6 h-6 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
              </svg>
            </div>
            <div className="ml-4">
              <h3 className="text-lg font-medium text-gray-900">Process Recording</h3>
              <p className="text-sm text-gray-500">Take attendance from a Zoom recording</p>
            </div>
          </div>
        </Link>

        <div className="card">
          <div className="flex items-center">
            <div className="p-3 bg-green-100 rounded-lg">
              <svg className="w-6 h-6 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
            </div>
            <div className="ml-4">
              <h3 className="text-lg font-medium text-gray-900">
                {sheetsLoading ? '...' : sheetsData?.total || 0} Sessions
              </h3>
              <p className="text-sm text-gray-500">Active attendance sheets</p>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="flex items-center">
            <div className="p-3 bg-purple-100 rounded-lg">
              <svg className="w-6 h-6 text-purple-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z" />
              </svg>
            </div>
            <div className="ml-4">
              <h3 className="text-lg font-medium text-gray-900">
                {recordingsLoading ? '...' : recordingsData?.total || 0} Recordings
              </h3>
              <p className="text-sm text-gray-500">Available in last 30 days</p>
            </div>
          </div>
        </div>
      </div>

      {/* Sessions List */}
      <div className="card">
        <div className="flex justify-between items-center mb-4">
          <h2 className="text-lg font-semibold text-gray-900">Sessions</h2>
        </div>

        {sheetsLoading ? (
          <div className="text-center py-8 text-gray-500">Loading sessions...</div>
        ) : sheetsData?.sheets.length === 0 ? (
          <div className="text-center py-8 text-gray-500">
            No sessions found. Process a recording to create one.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead>
                <tr>
                  <th className="table-header px-6 py-3">Session</th>
                  <th className="table-header px-6 py-3">Sheet Name</th>
                  <th className="table-header px-6 py-3">Actions</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {sheetsData?.sheets.map((sheet) => (
                  <tr key={sheet.id} className="hover:bg-gray-50">
                    <td className="px-6 py-4 whitespace-nowrap">
                      <span className="px-2 py-1 bg-blue-100 text-blue-800 text-sm font-medium rounded">
                        {sheet.session_code || 'N/A'}
                      </span>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">
                      {sheet.name}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm">
                      <div className="flex space-x-2">
                        <Link
                          to={`/session/${sheet.session_code}`}
                          className="text-blue-600 hover:text-blue-900"
                        >
                          View
                        </Link>
                        <a
                          href={sheet.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-green-600 hover:text-green-900"
                        >
                          Open Sheet
                        </a>
                        <Link
                          to={`/duplicates/${sheet.id}`}
                          className="text-orange-600 hover:text-orange-900"
                        >
                          Check Duplicates
                        </Link>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Recent Recordings */}
      <div className="card">
        <div className="flex justify-between items-center mb-4">
          <h2 className="text-lg font-semibold text-gray-900">Recent Recordings</h2>
          <Link to="/recordings" className="text-blue-600 hover:text-blue-800 text-sm">
            View All
          </Link>
        </div>

        {recordingsLoading ? (
          <div className="text-center py-8 text-gray-500">Loading recordings...</div>
        ) : recordingsData?.recordings.length === 0 ? (
          <div className="text-center py-8 text-gray-500">
            No recordings found in the last 30 days.
          </div>
        ) : (
          <div className="space-y-3">
            {recordingsData?.recordings.slice(0, 5).map((recording) => (
              <div
                key={recording.id}
                className="flex justify-between items-center p-3 bg-gray-50 rounded-lg"
              >
                <div>
                  <p className="font-medium text-gray-900">{recording.topic}</p>
                  <p className="text-sm text-gray-500">
                    {new Date(recording.start_time).toLocaleDateString()} - {recording.duration} min
                  </p>
                </div>
                <div className="flex items-center space-x-2">
                  {recording.session_code && (
                    <span className="px-2 py-1 bg-blue-100 text-blue-800 text-xs font-medium rounded">
                      Session {recording.session_code}
                    </span>
                  )}
                  <Link
                    to={`/recordings?id=${recording.meeting_id}`}
                    className="btn btn-primary text-sm"
                  >
                    Process
                  </Link>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
