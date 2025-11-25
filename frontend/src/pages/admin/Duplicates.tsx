import { useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { studentsApi, DuplicateMatch } from '../../services/api'

export default function DuplicatesPage() {
  const { spreadsheetId } = useParams<{ spreadsheetId: string }>()
  const queryClient = useQueryClient()

  const [selectedPair, setSelectedPair] = useState<DuplicateMatch | null>(null)
  const [keepRow, setKeepRow] = useState<number | null>(null)

  const { data: duplicatesData, isLoading } = useQuery({
    queryKey: ['duplicates', spreadsheetId],
    queryFn: () => studentsApi.findDuplicates(spreadsheetId!),
    enabled: !!spreadsheetId,
  })

  const { data: studentsData } = useQuery({
    queryKey: ['session-students', spreadsheetId],
    queryFn: () => studentsApi.getSessionStudents(spreadsheetId!),
    enabled: !!spreadsheetId,
  })

  const mergeMutation = useMutation({
    mutationFn: async ({ keepRow, mergeRow }: { keepRow: number; mergeRow: number }) => {
      return studentsApi.merge(spreadsheetId!, keepRow, mergeRow)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['duplicates', spreadsheetId] })
      queryClient.invalidateQueries({ queryKey: ['session-students', spreadsheetId] })
      setSelectedPair(null)
      setKeepRow(null)
    },
  })

  const handleMerge = () => {
    if (!selectedPair || !keepRow) return

    const mergeRow = keepRow === selectedPair.profile1.row
      ? selectedPair.profile2.row
      : selectedPair.profile1.row

    mergeMutation.mutate({ keepRow, mergeRow })
  }

  const getProfileByRow = (row: number) => {
    return studentsData?.profiles.find((p) => p.row_number === row)
  }

  if (isLoading) {
    return <div className="text-center py-8">Scanning for duplicates...</div>
  }

  const duplicates = duplicatesData?.duplicates || []

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Duplicate Detection</h1>
          <p className="mt-1 text-gray-600">
            Found {duplicates.length} potential duplicate{duplicates.length !== 1 ? 's' : ''}
          </p>
        </div>
        <Link to="/" className="btn btn-secondary">
          Back to Dashboard
        </Link>
      </div>

      {duplicates.length === 0 ? (
        <div className="card text-center py-12">
          <svg
            className="mx-auto h-12 w-12 text-green-500"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
            />
          </svg>
          <h3 className="mt-4 text-lg font-medium text-gray-900">No Duplicates Found</h3>
          <p className="mt-2 text-gray-500">All profiles appear to be unique.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Duplicates List */}
          <div className="space-y-4">
            <h2 className="font-semibold text-gray-900">Potential Duplicates</h2>
            {duplicates.map((dup, idx) => (
              <div
                key={idx}
                onClick={() => {
                  setSelectedPair(dup)
                  setKeepRow(null)
                }}
                className={`card cursor-pointer transition-colors ${
                  selectedPair === dup
                    ? 'ring-2 ring-blue-500'
                    : 'hover:bg-gray-50'
                }`}
              >
                <div className="flex justify-between items-start">
                  <div>
                    <div className="flex items-center space-x-2">
                      <span className="font-medium">{dup.profile1.name}</span>
                      <span className="text-gray-400">vs</span>
                      <span className="font-medium">{dup.profile2.name}</span>
                    </div>
                    <p className="text-sm text-gray-500 mt-1">{dup.reason}</p>
                  </div>
                  <div className="flex items-center">
                    <span
                      className={`px-2 py-1 rounded text-sm font-medium ${
                        dup.confidence >= 90
                          ? 'bg-red-100 text-red-800'
                          : dup.confidence >= 80
                          ? 'bg-orange-100 text-orange-800'
                          : 'bg-yellow-100 text-yellow-800'
                      }`}
                    >
                      {dup.confidence}% match
                    </span>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* Merge Panel */}
          <div className="card">
            {!selectedPair ? (
              <div className="text-center py-12 text-gray-500">
                Select a duplicate pair to review and merge
              </div>
            ) : (
              <div className="space-y-6">
                <h2 className="font-semibold text-gray-900">Merge Profiles</h2>
                <p className="text-sm text-gray-500">
                  Select which profile to keep. The other profile's attendance data will be merged in.
                </p>

                {/* Profile 1 */}
                <div
                  onClick={() => setKeepRow(selectedPair.profile1.row)}
                  className={`p-4 border-2 rounded-lg cursor-pointer ${
                    keepRow === selectedPair.profile1.row
                      ? 'border-blue-500 bg-blue-50'
                      : 'border-gray-200 hover:border-gray-300'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="font-medium">{selectedPair.profile1.name}</p>
                      {(() => {
                        const profile = getProfileByRow(selectedPair.profile1.row)
                        return profile ? (
                          <p className="text-sm text-gray-500">
                            Email: {profile.email || 'None'} | Row: {profile.row_number}
                          </p>
                        ) : null
                      })()}
                    </div>
                    {keepRow === selectedPair.profile1.row && (
                      <span className="text-blue-600 font-medium">Keep</span>
                    )}
                  </div>
                </div>

                {/* Profile 2 */}
                <div
                  onClick={() => setKeepRow(selectedPair.profile2.row)}
                  className={`p-4 border-2 rounded-lg cursor-pointer ${
                    keepRow === selectedPair.profile2.row
                      ? 'border-blue-500 bg-blue-50'
                      : 'border-gray-200 hover:border-gray-300'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="font-medium">{selectedPair.profile2.name}</p>
                      {(() => {
                        const profile = getProfileByRow(selectedPair.profile2.row)
                        return profile ? (
                          <p className="text-sm text-gray-500">
                            Email: {profile.email || 'None'} | Row: {profile.row_number}
                          </p>
                        ) : null
                      })()}
                    </div>
                    {keepRow === selectedPair.profile2.row && (
                      <span className="text-blue-600 font-medium">Keep</span>
                    )}
                  </div>
                </div>

                {/* Info */}
                <div className="p-3 bg-yellow-50 rounded-lg text-sm text-yellow-800">
                  <strong>Note:</strong> When merging, attendance data from both profiles
                  will be combined (keeping the higher value for each date). The profile
                  you don't select will be deleted.
                </div>

                {/* Merge Button */}
                <div className="flex space-x-2">
                  <button
                    onClick={() => {
                      setSelectedPair(null)
                      setKeepRow(null)
                    }}
                    className="btn btn-secondary flex-1"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleMerge}
                    disabled={!keepRow || mergeMutation.isPending}
                    className="btn btn-danger flex-1 disabled:opacity-50"
                  >
                    {mergeMutation.isPending ? 'Merging...' : 'Merge Profiles'}
                  </button>
                </div>

                {mergeMutation.isError && (
                  <p className="text-sm text-red-600">
                    Error merging profiles: {(mergeMutation.error as Error).message}
                  </p>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
