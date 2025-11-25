import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { studentsApi } from '../../services/api'

export default function StudentSearch() {
  const [searchQuery, setSearchQuery] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState('')

  // Use summary-based search for cleaner roster names and Zoom name matching
  const { data: searchResults, isLoading, isError } = useQuery({
    queryKey: ['student-summary-search', submittedQuery],
    queryFn: () => studentsApi.searchSummary(submittedQuery),
    enabled: submittedQuery.length >= 2,
  })

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    if (searchQuery.trim().length >= 2) {
      setSubmittedQuery(searchQuery.trim())
    }
  }

  return (
    <div className="max-w-2xl mx-auto space-y-8">
      <div className="text-center">
        <h1 className="text-3xl font-bold text-gray-900">Find Your Attendance</h1>
        <p className="mt-2 text-gray-600">
          Search by your name or email to view your attendance records
        </p>
      </div>

      {/* Search Form */}
      <form onSubmit={handleSearch} className="card">
        <div className="flex space-x-2">
          <input
            type="text"
            placeholder="Enter your name or email..."
            className="input flex-1"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            autoFocus
          />
          <button
            type="submit"
            className="btn btn-primary"
            disabled={searchQuery.trim().length < 2}
          >
            Search
          </button>
        </div>
        <p className="mt-2 text-sm text-gray-500">
          Enter at least 2 characters to search
        </p>
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
                No profiles found matching "{submittedQuery}"
              </p>
              <p className="text-sm text-gray-400 mt-2">
                Try searching with a different name or check with your instructor
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              {searchResults.results.map((result) => (
                <Link
                  key={`${result.session_code}-${result.row_number}`}
                  to={`/student/summary/${result.session_code}/${result.row_number}`}
                  className="card block hover:shadow-lg transition-shadow"
                >
                  <div className="flex justify-between items-start">
                    <div>
                      <h3 className="font-medium text-gray-900">
                        {result.first_name} {result.last_name}
                      </h3>
                      {result.student_id && (
                        <p className="text-sm text-gray-500">ID: {result.student_id}</p>
                      )}
                      {result.known_zoom_names && result.known_zoom_names.length > 0 && (
                        <p className="text-xs text-gray-400 mt-1">
                          Also known as: {result.known_zoom_names.join(', ')}
                        </p>
                      )}
                      <p className="text-sm text-blue-600 mt-1">
                        {result.session_name}
                      </p>
                    </div>
                    <span className="px-2 py-1 bg-blue-100 text-blue-800 text-sm font-medium rounded">
                      Session {result.session_code || 'N/A'}
                    </span>
                  </div>
                </Link>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Initial State */}
      {!submittedQuery && !isLoading && (
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
          <p className="mt-4">Enter your name or email above to find your attendance records</p>
        </div>
      )}
    </div>
  )
}
