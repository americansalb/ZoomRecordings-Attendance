import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { studentsApi } from '../../services/api'

export default function StudentSearch() {
  const [firstName, setFirstName] = useState('')
  const [sessionCode, setSessionCode] = useState('')
  const [submittedSearch, setSubmittedSearch] = useState<{ firstName: string; sessionCode: string } | null>(null)
  const [showHelp, setShowHelp] = useState(false)

  const { data: searchResults, isLoading, isError } = useQuery({
    queryKey: ['student-lookup', submittedSearch?.firstName, submittedSearch?.sessionCode],
    queryFn: () => studentsApi.lookup(submittedSearch!.firstName, submittedSearch!.sessionCode),
    enabled: !!submittedSearch,
  })

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    if (firstName.trim().length >= 1 && sessionCode.trim().length === 3) {
      setSubmittedSearch({ firstName: firstName.trim(), sessionCode: sessionCode.trim() })
    }
  }

  const isValidSessionCode = /^\d{3}$/.test(sessionCode)
  const canSearch = firstName.trim().length >= 1 && isValidSessionCode

  return (
    <div className="max-w-2xl mx-auto space-y-8">
      <div className="text-center">
        <h1 className="text-3xl font-bold text-gray-900">Find Your Attendance</h1>
        <p className="mt-2 text-gray-600">
          Enter your first name and session number to view your attendance records
        </p>
      </div>

      {/* Search Form */}
      <form onSubmit={handleSearch} className="card space-y-4">
        {/* First Name Input */}
        <div>
          <label htmlFor="firstName" className="block text-sm font-medium text-gray-700 mb-1">
            First Name
          </label>
          <input
            type="text"
            id="firstName"
            placeholder="Enter your first name..."
            className="input w-full"
            value={firstName}
            onChange={(e) => setFirstName(e.target.value)}
            autoFocus
          />
        </div>

        {/* Session Code Input */}
        <div>
          <div className="flex items-center gap-2 mb-1">
            <label htmlFor="sessionCode" className="block text-sm font-medium text-gray-700">
              Session #
            </label>
            <button
              type="button"
              onClick={() => setShowHelp(!showHelp)}
              className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-gray-200 text-gray-600 hover:bg-gray-300 text-xs font-bold"
              aria-label="Help"
            >
              ?
            </button>
          </div>
          {showHelp && (
            <div className="mb-2 p-3 bg-blue-50 text-blue-800 text-sm rounded-lg">
              You can find your 3-digit Session # in your registration email and at the top of Google Classroom.
            </div>
          )}
          <input
            type="text"
            id="sessionCode"
            placeholder="e.g., 129"
            className="input w-full"
            value={sessionCode}
            onChange={(e) => {
              // Only allow digits, max 3 characters
              const value = e.target.value.replace(/\D/g, '').slice(0, 3)
              setSessionCode(value)
            }}
            maxLength={3}
          />
          {sessionCode && !isValidSessionCode && (
            <p className="mt-1 text-sm text-red-500">Please enter a 3-digit session number</p>
          )}
        </div>

        <button
          type="submit"
          className="btn btn-primary w-full"
          disabled={!canSearch}
        >
          Search
        </button>
      </form>

      {/* Loading State */}
      {isLoading && (
        <div className="text-center py-8">
          <div className="inline-block animate-spin rounded-full h-8 w-8 border-4 border-blue-500 border-t-transparent"></div>
          <p className="mt-2 text-gray-500">Searching...</p>
        </div>
      )}

      {/* Error State */}
      {isError && (
        <div className="card bg-red-50 text-red-700 text-center">
          An error occurred while searching. Please try again.
        </div>
      )}

      {/* Results */}
      {searchResults && (
        <div className="space-y-4">
          <h2 className="text-lg font-semibold text-gray-900">
            {searchResults.total === 0
              ? 'No results found'
              : `Found ${searchResults.total} result${searchResults.total !== 1 ? 's' : ''}`}
          </h2>

          {searchResults.total === 0 ? (
            <div className="card text-center py-8">
              <svg
                className="mx-auto h-12 w-12 text-gray-400"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M9.172 16.172a4 4 0 015.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                />
              </svg>
              <p className="mt-4 text-gray-500">
                No students found matching "{submittedSearch?.firstName}" in Session {submittedSearch?.sessionCode}
              </p>
              <p className="text-sm text-gray-400 mt-2">
                Please check your first name spelling and session number
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              <p className="text-sm text-gray-500">
                Select your record below:
              </p>
              {searchResults.results.map((result) => (
                <Link
                  key={`${result.session_code}-${result.row_number}`}
                  to={`/student/summary/${result.session_code}/${result.row_number}`}
                  className="card block hover:shadow-lg hover:border-teal-300 transition-all border-2 border-transparent"
                >
                  <div className="flex justify-between items-center">
                    <div>
                      <h3 className="font-medium text-gray-900 text-lg">
                        {result.display_name}
                      </h3>
                    </div>
                    <svg
                      className="w-5 h-5 text-gray-400"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M9 5l7 7-7 7"
                      />
                    </svg>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Initial State */}
      {!submittedSearch && !isLoading && (
        <div className="text-center py-8 text-gray-500">
          <svg
            className="mx-auto h-16 w-16 text-gray-300"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={1.5}
              d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
            />
          </svg>
          <p className="mt-4">Enter your first name and session number above to find your attendance records</p>
        </div>
      )}
    </div>
  )
}
