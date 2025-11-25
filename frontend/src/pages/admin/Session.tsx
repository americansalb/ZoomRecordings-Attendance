import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { sheetsApi, studentsApi, attendanceApi, Profile } from '../../services/api'

export default function SessionPage() {
  const { sessionCode } = useParams<{ sessionCode: string }>()
  const queryClient = useQueryClient()

  const [editingCell, setEditingCell] = useState<{
    row: number
    col: string
    type: 'attendance' | 'participation'
  } | null>(null)
  const [editValue, setEditValue] = useState('')
  const [editingProfile, setEditingProfile] = useState<Profile | null>(null)

  const { data: sheetData, isLoading: sheetLoading } = useQuery({
    queryKey: ['sheet', sessionCode],
    queryFn: () => sheetsApi.getBySession(sessionCode!),
    enabled: !!sessionCode,
  })

  const { data: studentsData, isLoading: studentsLoading } = useQuery({
    queryKey: ['session-students', sessionCode],
    queryFn: () => studentsApi.getSessionStudents(sessionCode!),
    enabled: !!sessionCode && !!sheetData,
  })

  const updateMutation = useMutation({
    mutationFn: async ({ row, date, attendance, participation }: {
      row: number
      date: string
      attendance?: number
      participation?: number
    }) => {
      return attendanceApi.update(sessionCode!, row, date, attendance, participation)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['session-students', sessionCode] })
      setEditingCell(null)
    },
  })

  const updateProfileMutation = useMutation({
    mutationFn: async (profile: Profile) => {
      return studentsApi.updateProfile(
        sessionCode!,
        profile.row_number,
        profile.first_name,
        profile.last_name,
        profile.email
      )
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['session-students', sessionCode] })
      setEditingProfile(null)
    },
  })

  const handleCellClick = (row: number, date: string, type: 'attendance' | 'participation', currentValue: number | string) => {
    setEditingCell({ row, col: date, type })
    setEditValue(String(currentValue || 0))
  }

  const handleCellSave = () => {
    if (!editingCell) return

    const value = parseInt(editValue) || 0
    if (editingCell.type === 'attendance') {
      updateMutation.mutate({
        row: editingCell.row,
        date: editingCell.col,
        attendance: value,
      })
    } else {
      updateMutation.mutate({
        row: editingCell.row,
        date: editingCell.col,
        participation: value,
      })
    }
  }

  const handleProfileSave = () => {
    if (!editingProfile) return
    updateProfileMutation.mutate(editingProfile)
  }

  if (sheetLoading || studentsLoading) {
    return <div className="text-center py-8">Loading...</div>
  }

  if (!sheetData) {
    return (
      <div className="text-center py-8">
        <p className="text-gray-500">Session {sessionCode} not found</p>
        <Link to="/" className="text-blue-600 hover:text-blue-800">
          Back to Dashboard
        </Link>
      </div>
    )
  }

  const dates = studentsData?.dates || []
  const profiles = studentsData?.profiles || []

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Session {sessionCode}</h1>
          <p className="mt-1 text-gray-600">{sheetData.name}</p>
        </div>
        <div className="flex space-x-2">
          <Link
            to={`/duplicates/${sessionCode}`}
            className="btn btn-secondary"
          >
            Check Duplicates
          </Link>
          <a
            href={sheetData.spreadsheet_url}
            target="_blank"
            rel="noopener noreferrer"
            className="btn btn-primary"
          >
            Open Sheet
          </a>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-3 gap-4">
        <div className="card">
          <p className="text-sm text-gray-500">Total Students</p>
          <p className="text-2xl font-bold text-gray-900">{profiles.length}</p>
        </div>
        <div className="card">
          <p className="text-sm text-gray-500">Meeting Dates</p>
          <p className="text-2xl font-bold text-gray-900">{dates.length}</p>
        </div>
        <div className="card">
          <p className="text-sm text-gray-500">Latest Date</p>
          <p className="text-2xl font-bold text-gray-900">{dates[dates.length - 1] || 'N/A'}</p>
        </div>
      </div>

      {/* Attendance Table */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="sticky left-0 bg-gray-50 px-4 py-3 text-left text-xs font-semibold text-gray-600 uppercase z-10">
                  Name
                </th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-600 uppercase">
                  Email
                </th>
                {dates.map((date) => (
                  <th key={date} className="px-2 py-3 text-center text-xs font-semibold text-gray-600 uppercase" colSpan={2}>
                    {date}
                  </th>
                ))}
              </tr>
              <tr className="bg-gray-100">
                <th className="sticky left-0 bg-gray-100 px-4 py-2 z-10"></th>
                <th className="px-4 py-2"></th>
                {dates.map((date) => (
                  <>
                    <th key={`${date}-att`} className="px-2 py-2 text-center text-xs text-gray-500">
                      Att
                    </th>
                    <th key={`${date}-part`} className="px-2 py-2 text-center text-xs text-gray-500">
                      Part
                    </th>
                  </>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {profiles.map((profile) => (
                <tr key={profile.row_number} className="hover:bg-gray-50">
                  <td className="sticky left-0 bg-white px-4 py-3 whitespace-nowrap z-10">
                    <button
                      onClick={() => setEditingProfile({ ...profile })}
                      className="text-left hover:text-blue-600"
                    >
                      <div className="font-medium text-gray-900">
                        {profile.first_name} {profile.last_name}
                      </div>
                    </button>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap text-sm text-gray-500">
                    {profile.email || '-'}
                  </td>
                  {dates.map((date) => {
                    const attKey = `${date} Attendance`
                    const partKey = `${date} Participation`
                    const attValue = profile.attendance[attKey] || 0
                    const partValue = profile.attendance[partKey] || 0

                    return (
                      <>
                        <td
                          key={`${profile.row_number}-${date}-att`}
                          className="px-2 py-3 text-center text-sm cursor-pointer hover:bg-blue-50"
                          onClick={() => handleCellClick(profile.row_number, date, 'attendance', attValue)}
                        >
                          {editingCell?.row === profile.row_number &&
                           editingCell?.col === date &&
                           editingCell?.type === 'attendance' ? (
                            <input
                              type="number"
                              className="w-16 px-1 py-0.5 border rounded text-center"
                              value={editValue}
                              onChange={(e) => setEditValue(e.target.value)}
                              onBlur={handleCellSave}
                              onKeyDown={(e) => e.key === 'Enter' && handleCellSave()}
                              autoFocus
                            />
                          ) : (
                            <span className={attValue ? 'text-green-600' : 'text-gray-400'}>
                              {attValue}
                            </span>
                          )}
                        </td>
                        <td
                          key={`${profile.row_number}-${date}-part`}
                          className="px-2 py-3 text-center text-sm cursor-pointer hover:bg-purple-50"
                          onClick={() => handleCellClick(profile.row_number, date, 'participation', partValue)}
                        >
                          {editingCell?.row === profile.row_number &&
                           editingCell?.col === date &&
                           editingCell?.type === 'participation' ? (
                            <input
                              type="number"
                              className="w-16 px-1 py-0.5 border rounded text-center"
                              value={editValue}
                              onChange={(e) => setEditValue(e.target.value)}
                              onBlur={handleCellSave}
                              onKeyDown={(e) => e.key === 'Enter' && handleCellSave()}
                              autoFocus
                            />
                          ) : (
                            <span className={partValue ? 'text-purple-600' : 'text-gray-400'}>
                              {partValue}
                            </span>
                          )}
                        </td>
                      </>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Edit Profile Modal */}
      {editingProfile && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 w-full max-w-md">
            <h2 className="text-lg font-semibold mb-4">Edit Profile</h2>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  First Name
                </label>
                <input
                  type="text"
                  className="input"
                  value={editingProfile.first_name}
                  onChange={(e) => setEditingProfile({ ...editingProfile, first_name: e.target.value })}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Last Name
                </label>
                <input
                  type="text"
                  className="input"
                  value={editingProfile.last_name}
                  onChange={(e) => setEditingProfile({ ...editingProfile, last_name: e.target.value })}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Email
                </label>
                <input
                  type="email"
                  className="input"
                  value={editingProfile.email}
                  onChange={(e) => setEditingProfile({ ...editingProfile, email: e.target.value })}
                />
              </div>
            </div>
            <div className="flex justify-end space-x-2 mt-6">
              <button
                onClick={() => setEditingProfile(null)}
                className="btn btn-secondary"
              >
                Cancel
              </button>
              <button
                onClick={handleProfileSave}
                className="btn btn-primary"
                disabled={updateProfileMutation.isPending}
              >
                {updateProfileMutation.isPending ? 'Saving...' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
