/**
 * The attendance bot console used to live here, duplicating the one in the
 * academy admin. Two consoles meant two half-truths about the same bot, so
 * this one was retired. The route stays so old bookmarks land on a signpost
 * instead of a 404.
 */
const CONSOLE_URL = 'https://academy.aalb.org/admin/zoom-bot.html'

export default function LiveTutorPage() {
  return (
    <div className="max-w-xl mx-auto mt-16 bg-white rounded-lg shadow-sm border border-gray-200 p-8 text-center">
      <h1 className="text-2xl font-bold text-gray-900 mb-3">This console moved</h1>
      <p className="text-gray-600 mb-2">
        The attendance bot is run from one place now: the academy admin, under
        Classes, then Attendance bot. Sending the bot, live observations, camera
        messages, face checks and attendance reports all live there.
      </p>
      <p className="text-gray-600 mb-6">
        This page no longer controls anything.
      </p>
      <a
        href={CONSOLE_URL}
        className="inline-block px-5 py-3 rounded-md bg-blue-600 text-white font-medium hover:bg-blue-700"
      >
        Open the attendance bot console
      </a>
    </div>
  )
}
