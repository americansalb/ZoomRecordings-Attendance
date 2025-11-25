import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { mappingsApi, sheetsApi, NameMapping, RosterStudent } from '../../services/api'

export default function NameMappingsPage() {
  const queryClient = useQueryClient()
  const [selectedSession, setSelectedSession] = useState<string>('')
  const [newMapping, setNewMapping] = useState({
    zoom_name: '',
    student_id: '',
    first_name: '',
    last_name: '',
    session_code: '',
  })
  const [showAddForm, setShowAddForm] = useState(false)
  const [selectedRosterStudent, setSelectedRosterStudent] = useState<RosterStudent | null>(null)

  // Fetch sessions for dropdown
  const { data: sessionsData } = useQuery({
    queryKey: ['sheets'],
    queryFn: sheetsApi.list,
  })

  // Fetch existing mappings
  const { data: mappingsData, isLoading: mappingsLoading } = useQuery({
    queryKey: ['mappings', selectedSession || 'all'],
    queryFn: () => mappingsApi.list(selectedSession || undefined),
  })

  // Fetch roster when session is selected for new mapping
  const { data: rosterData, isLoading: rosterLoading } = useQuery({
    queryKey: ['roster', newMapping.session_code],
    queryFn: () => mappingsApi.getRoster(newMapping.session_code),
    enabled: !!newMapping.session_code,
  })

  // Create mapping mutation
  const createMutation = useMutation({
    mutationFn: mappingsApi.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mappings'] })
      setShowAddForm(false)
      setNewMapping({
        zoom_name: '',
        student_id: '',
        first_name: '',
        last_name: '',
        session_code: '',
      })
      setSelectedRosterStudent(null)
    },
  })

  // Delete mapping mutation
  const deleteMutation = useMutation({
    mutationFn: mappingsApi.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mappings'] })
    },
  })

  const handleSelectRosterStudent = (student: RosterStudent) => {
    setSelectedRosterStudent(student)
    setNewMapping((prev) => ({
      ...prev,
      student_id: student.student_id,
      first_name: student.first_name,
      last_name: student.last_name,
    }))
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!newMapping.zoom_name || !newMapping.first_name || !newMapping.last_name) {
      alert('Please fill in all required fields')
      return
    }
    createMutation.mutate(newMapping)
  }

  const handleDelete = (zoomName: string) => {
    if (confirm(`Delete mapping for "${zoomName}"?`)) {
      deleteMutation.mutate(zoomName)
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-start">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Name Mappings</h1>
          <p className="mt-1 text-gray-600">
            Map Zoom display names to roster students for accurate attendance tracking
          </p>
        </div>
        <button
          onClick={() => setShowAddForm(!showAddForm)}
          className="btn btn-primary"
        >
          {showAddForm ? 'Cancel' : 'Add Mapping'}
        </button>
      </div>

      {/* Add Mapping Form */}
      {showAddForm && (
        <div className="card bg-blue-50 border-blue-200">
          <h2 className="text-lg font-semibold text-gray-900 mb-4">New Name Mapping</h2>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {/* Session Code */}
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Session (optional - leave blank for all sessions)
                </label>
                <select
                  className="input"
                  value={newMapping.session_code}
                  onChange={(e) =>
                    setNewMapping((prev) => ({ ...prev, session_code: e.target.value }))
                  }
                >
                  <option value="">All Sessions</option>
                  {(sessionsData?.sessions ?? []).map((session) => (
                    <option key={session.session_code} value={session.session_code}>
                      Session {session.session_code}
                    </option>
                  ))}
                </select>
              </div>

              {/* Zoom Name */}
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Zoom Display Name *
                </label>
                <input
                  type="text"
                  className="input"
                  placeholder="e.g., Jamie R (Spanish)"
                  value={newMapping.zoom_name}
                  onChange={(e) =>
                    setNewMapping((prev) => ({ ...prev, zoom_name: e.target.value }))
                  }
                  required
                />
              </div>
            </div>

            {/* Roster Selection */}
            {newMapping.session_code && (
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Select from Roster
                </label>
                {rosterLoading ? (
                  <p className="text-sm text-gray-500">Loading roster...</p>
                ) : (rosterData?.roster?.length ?? 0) === 0 ? (
                  <p className="text-sm text-gray-500">No roster found for this session</p>
                ) : (
                  <div className="max-h-48 overflow-y-auto border rounded-lg bg-white">
                    {rosterData?.roster.map((student) => (
                      <div
                        key={student.student_id}
                        onClick={() => handleSelectRosterStudent(student)}
                        className={`p-2 cursor-pointer border-b last:border-b-0 ${
                          selectedRosterStudent?.student_id === student.student_id
                            ? 'bg-blue-100'
                            : 'hover:bg-gray-50'
                        }`}
                      >
                        <span className="font-medium">{student.first_name} {student.last_name}</span>
                        <span className="text-sm text-gray-500 ml-2">ID: {student.student_id}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Manual Entry */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  First Name *
                </label>
                <input
                  type="text"
                  className="input"
                  value={newMapping.first_name}
                  onChange={(e) =>
                    setNewMapping((prev) => ({ ...prev, first_name: e.target.value }))
                  }
                  required
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Last Name *
                </label>
                <input
                  type="text"
                  className="input"
                  value={newMapping.last_name}
                  onChange={(e) =>
                    setNewMapping((prev) => ({ ...prev, last_name: e.target.value }))
                  }
                  required
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Student ID
                </label>
                <input
                  type="text"
                  className="input"
                  value={newMapping.student_id}
                  onChange={(e) =>
                    setNewMapping((prev) => ({ ...prev, student_id: e.target.value }))
                  }
                />
              </div>
            </div>

            <div className="flex justify-end">
              <button
                type="submit"
                disabled={createMutation.isPending}
                className="btn btn-primary"
              >
                {createMutation.isPending ? 'Saving...' : 'Save Mapping'}
              </button>
            </div>

            {createMutation.isError && (
              <p className="text-sm text-red-600">
                Error: {(createMutation.error as Error).message}
              </p>
            )}
          </form>
        </div>
      )}

      {/* Filter by Session */}
      <div className="flex items-center space-x-4">
        <label className="text-sm font-medium text-gray-700">Filter by Session:</label>
        <select
          className="input w-48"
          value={selectedSession}
          onChange={(e) => setSelectedSession(e.target.value)}
        >
          <option value="">All Sessions</option>
          {(sessionsData?.sessions ?? []).map((session) => (
            <option key={session.session_code} value={session.session_code}>
              Session {session.session_code}
            </option>
          ))}
        </select>
      </div>

      {/* Mappings List */}
      <div className="card">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">
          Existing Mappings ({mappingsData?.total ?? 0})
        </h2>

        {mappingsLoading ? (
          <div className="text-center py-8 text-gray-500">Loading mappings...</div>
        ) : (mappingsData?.mappings?.length ?? 0) === 0 ? (
          <div className="text-center py-8 text-gray-500">
            No name mappings found. Add one to map Zoom names to roster students.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead>
                <tr>
                  <th className="table-header px-4 py-3">Zoom Name</th>
                  <th className="table-header px-4 py-3">Maps To</th>
                  <th className="table-header px-4 py-3">Student ID</th>
                  <th className="table-header px-4 py-3">Session</th>
                  <th className="table-header px-4 py-3">Created</th>
                  <th className="table-header px-4 py-3">Actions</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {(mappingsData?.mappings ?? []).map((mapping: NameMapping, idx: number) => (
                  <tr key={`${mapping.zoom_name}-${idx}`} className="hover:bg-gray-50">
                    <td className="px-4 py-3 text-sm">
                      <span className="font-medium text-orange-600">{mapping.zoom_name}</span>
                    </td>
                    <td className="px-4 py-3 text-sm">
                      <span className="font-medium text-green-600">
                        {mapping.first_name} {mapping.last_name}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-500">
                      {mapping.student_id || '-'}
                    </td>
                    <td className="px-4 py-3 text-sm">
                      {mapping.session_code ? (
                        <span className="px-2 py-1 bg-blue-100 text-blue-800 text-xs font-medium rounded">
                          {mapping.session_code}
                        </span>
                      ) : (
                        <span className="text-gray-400">All</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-sm text-gray-500">
                      {mapping.created_at
                        ? new Date(mapping.created_at).toLocaleDateString()
                        : '-'}
                    </td>
                    <td className="px-4 py-3 text-sm">
                      <button
                        onClick={() => handleDelete(mapping.zoom_name)}
                        disabled={deleteMutation.isPending}
                        className="text-red-600 hover:text-red-800"
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Help Text */}
      <div className="bg-gray-50 rounded-lg p-4 text-sm text-gray-600">
        <h3 className="font-medium text-gray-900 mb-2">How Name Mappings Work</h3>
        <ul className="list-disc list-inside space-y-1">
          <li>
            <strong>Zoom Name:</strong> The exact name shown in Zoom (e.g., "Jamie R (Spanish)")
          </li>
          <li>
            <strong>Maps To:</strong> The canonical student name to use in attendance records
          </li>
          <li>
            <strong>Session:</strong> Leave blank to apply to all sessions, or select a specific session
          </li>
          <li>
            Mappings take priority over fuzzy matching - if a Zoom name matches a mapping exactly, it will use that mapping
          </li>
        </ul>
      </div>
    </div>
  )
}
