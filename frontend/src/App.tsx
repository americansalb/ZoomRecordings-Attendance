import { Routes, Route, Link, useLocation } from 'react-router-dom'

// Admin Pages
import AdminDashboard from './pages/admin/Dashboard'
import RecordingsPage from './pages/admin/Recordings'
import SessionPage from './pages/admin/Session'
import DuplicatesPage from './pages/admin/Duplicates'

// Student Pages
import StudentSearch from './pages/student/Search'
import StudentProfile from './pages/student/Profile'

function App() {
  const location = useLocation()
  const isAdmin = !location.pathname.startsWith('/student')

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Navigation */}
      <nav className="bg-white shadow-sm">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between h-16">
            <div className="flex items-center">
              <Link to="/" className="text-xl font-bold text-blue-600">
                Attendance Tracker
              </Link>

              <div className="ml-10 flex space-x-4">
                <Link
                  to="/"
                  className={`px-3 py-2 rounded-md text-sm font-medium ${
                    isAdmin ? 'text-blue-600 bg-blue-50' : 'text-gray-600 hover:text-gray-900'
                  }`}
                >
                  Admin
                </Link>
                <Link
                  to="/student"
                  className={`px-3 py-2 rounded-md text-sm font-medium ${
                    !isAdmin ? 'text-blue-600 bg-blue-50' : 'text-gray-600 hover:text-gray-900'
                  }`}
                >
                  Student
                </Link>
              </div>
            </div>
          </div>
        </div>
      </nav>

      {/* Main Content */}
      <main className="max-w-7xl mx-auto py-6 px-4 sm:px-6 lg:px-8">
        <Routes>
          {/* Admin Routes */}
          <Route path="/" element={<AdminDashboard />} />
          <Route path="/recordings" element={<RecordingsPage />} />
          <Route path="/session/:sessionCode" element={<SessionPage />} />
          <Route path="/duplicates/:sessionCode" element={<DuplicatesPage />} />

          {/* Student Routes */}
          <Route path="/student" element={<StudentSearch />} />
          <Route path="/student/profile/:sessionCode/:rowNumber" element={<StudentProfile />} />
        </Routes>
      </main>
    </div>
  )
}

export default App
