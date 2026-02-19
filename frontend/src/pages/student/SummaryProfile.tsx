import { useParams, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { studentsApi } from '../../services/api'

export default function StudentSummaryProfile() {
  const { sessionCode, rowNumber } = useParams<{
    sessionCode: string
    rowNumber: string
  }>()

  const { data: profile, isLoading, isError } = useQuery({
    queryKey: ['student-summary-profile', sessionCode, rowNumber],
    queryFn: () => studentsApi.getSummaryProfile(sessionCode!, parseInt(rowNumber!)),
    enabled: !!sessionCode && !!rowNumber,
  })

  if (isLoading) {
    return (
      <div className="text-center py-16">
        <div className="inline-block animate-spin rounded-full h-8 w-8 border-4 border-blue-500 border-t-transparent"></div>
        <p className="mt-2 text-gray-500">Loading profile...</p>
      </div>
    )
  }

  if (isError || !profile) {
    return (
      <div className="max-w-2xl mx-auto text-center py-16">
        <svg
          className="mx-auto h-12 w-12 text-red-500"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
          />
        </svg>
        <h2 className="mt-4 text-xl font-medium text-gray-900">Profile Not Found</h2>
        <p className="mt-2 text-gray-500">The profile you're looking for doesn't exist.</p>
        <Link to="/student" className="mt-4 inline-block text-blue-600 hover:text-blue-800">
          Back to Search
        </Link>
      </div>
    )
  }

  // Parse attendance data into structured format
  const attendanceData: Array<{
    date: string
    attendance: number
  }> = []

  // Use the dates array and attendance object
  const dates = profile.dates || Object.keys(profile.attendance || {})
  dates.forEach((date) => {
    const attendance = profile.attendance?.[date]
    if (attendance !== undefined) {
      attendanceData.push({
        date,
        attendance: typeof attendance === 'number' ? attendance : 0,
      })
    }
  })

  // Sort by date
  attendanceData.sort((a, b) => {
    const [aMonth, aDay] = a.date.split('/').map(Number)
    const [bMonth, bDay] = b.date.split('/').map(Number)
    if (aMonth !== bMonth) return aMonth - bMonth
    return aDay - bDay
  })

  // Check if needs review (has warning flag)
  const needsReview = profile.first_name.startsWith('⚠️')
  const displayFirstName = needsReview ? profile.first_name.replace('⚠️ ', '') : profile.first_name

  return (
    <div className="max-w-3xl mx-auto space-y-6">
      {/* Back Link */}
      <Link
        to="/student"
        className="inline-flex items-center text-blue-600 hover:text-blue-800"
      >
        <svg
          className="w-4 h-4 mr-1"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M15 19l-7-7 7-7"
          />
        </svg>
        Back to Search
      </Link>

      {/* Profile Header */}
      <div className="card">
        <div className="flex items-center space-x-4">
          <div className="w-16 h-16 bg-teal-100 rounded-full flex items-center justify-center">
            <span className="text-2xl font-bold text-teal-600">
              {displayFirstName.charAt(0)}
              {profile.last_name.charAt(0)}
            </span>
          </div>
          <div className="flex-1">
            {/* Student ID - Prominent Display */}
            {profile.student_id && (
              <div className="mb-2">
                <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">Student ID</p>
                <div className="text-4xl font-bold tracking-wider">
                  <span style={{ color: '#0D9488' }}>{profile.student_id.slice(0, 3)}</span>
                  <span style={{ color: '#5EEAD4' }}>{profile.student_id.slice(3, 5)}</span>
                </div>
              </div>
            )}
            <div className="flex items-center gap-2">
              <h1 className="text-xl font-semibold text-gray-900">
                {displayFirstName} {profile.last_name}
              </h1>
              {needsReview && (
                <span className="px-2 py-1 bg-yellow-100 text-yellow-800 text-xs font-medium rounded">
                  Needs Review
                </span>
              )}
            </div>
            <p className="text-sm text-gray-500">Session {sessionCode}</p>
          </div>
        </div>

        {/* Known Zoom Names */}
        {profile.known_zoom_names && profile.known_zoom_names.length > 0 && (
          <div className="mt-4 pt-4 border-t border-gray-200">
            <p className="text-sm font-medium text-gray-700 mb-2">Known Zoom Names:</p>
            <div className="flex flex-wrap gap-2">
              {profile.known_zoom_names.map((name, idx) => (
                <span
                  key={idx}
                  className="px-2 py-1 bg-gray-100 text-gray-700 text-sm rounded"
                >
                  {name}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Summary Stats */}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
        <div className="card text-center">
          <p className="text-sm text-gray-500">Sessions Attended</p>
          <p className="text-3xl font-bold text-gray-900">
            {profile.summary.total_sessions}
          </p>
        </div>
        <div className="card text-center">
          <p className="text-sm text-gray-500">Total Attendance</p>
          <p className="text-3xl font-bold text-green-600">
            {profile.summary.total_attendance_minutes}
            <span className="text-sm font-normal text-gray-500"> min</span>
          </p>
        </div>
        <div className="card text-center">
          <p className="text-sm text-gray-500">Avg per Session</p>
          <p className="text-3xl font-bold text-blue-600">
            {Math.round(profile.summary.average_attendance)}
            <span className="text-sm font-normal text-gray-500"> min</span>
          </p>
        </div>
      </div>

      {/* Attendance History */}
      <div className="card">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">
          Attendance History
        </h2>

        {attendanceData.length === 0 ? (
          <p className="text-center py-8 text-gray-500">
            No attendance records yet
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead>
                <tr>
                  <th className="table-header px-4 py-3">Date</th>
                  <th className="table-header px-4 py-3 text-right">Attendance (min)</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200">
                {attendanceData.map((record) => (
                  <tr key={record.date} className="hover:bg-gray-50">
                    <td className="px-4 py-3 font-medium text-gray-900">
                      {record.date}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <span
                        className={`px-2 py-1 rounded ${
                          record.attendance > 0
                            ? 'bg-green-100 text-green-800'
                            : 'bg-gray-100 text-gray-500'
                        }`}
                      >
                        {record.attendance}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot className="bg-gray-50">
                <tr>
                  <td className="px-4 py-3 font-semibold text-gray-900">Total</td>
                  <td className="px-4 py-3 text-right font-semibold text-green-600">
                    {profile.summary.total_attendance_minutes}
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
        )}
      </div>

      {/* Visual Progress */}
      {attendanceData.length > 0 && (
        <div className="card">
          <h2 className="text-lg font-semibold text-gray-900 mb-4">
            Attendance Overview
          </h2>
          <div className="space-y-3">
            {attendanceData.map((record) => {
              const maxMinutes = 60 // Assume 60 min sessions for visualization
              const attendancePercent = Math.min(100, (record.attendance / maxMinutes) * 100)

              return (
                <div key={record.date}>
                  <div className="flex justify-between text-sm mb-1">
                    <span className="font-medium">{record.date}</span>
                    <span className="text-gray-500">
                      {record.attendance} min attended
                    </span>
                  </div>
                  <div className="h-4 bg-gray-100 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-green-500 rounded-full"
                      style={{ width: `${attendancePercent}%` }}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
