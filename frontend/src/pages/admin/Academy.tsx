/**
 * Where to add an old recording to a class.
 *
 * The screen itself is on the academy, and it has to be. This server and the
 * academy are different origins, the academy's login cookie is sameSite lax, so
 * it is never sent on a request from here. A page here could only work by
 * copying a shared secret between two Render services, or by loosening that
 * cookie for every user on the site, and neither is worth it to save a click.
 *
 * Same origin over there means the admin login already in the browser just
 * works, with nothing configured anywhere. So this tab is the signpost, and
 * says plainly what is on the other side of it.
 */
const ACADEMY_URL = (
  (import.meta as any).env?.VITE_ACADEMY_URL || 'https://academy.aalb.org'
).replace(/\/$/, '')

const SCREEN = `${ACADEMY_URL}/admin/class-recordings`

export default function AcademyPage() {
  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-bold text-gray-900">Add recordings to the academy</h1>
      <p className="text-gray-600 mt-2">
        Every recording the archive holds, with the class night it matches, and a
        button to add the ones that fit. Matched by the session number in the Zoom
        title, then by which night the recording's hours actually cover.
      </p>
      <p className="text-gray-600 mt-3">
        The screen runs on the academy so it can use the admin login you already
        have there. Nothing to set up.
      </p>

      <a
        href={SCREEN}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-block mt-6 px-5 py-2.5 rounded-md bg-blue-600 text-white
                   font-medium hover:bg-blue-700"
      >
        Open it on the academy
      </a>

      <div className="mt-8 border-t border-gray-200 pt-6">
        <h2 className="font-semibold text-gray-900 mb-2">What happens when you add one</h2>
        <ul className="text-gray-600 space-y-1 list-disc pl-5">
          <li>The class is cut out: a minute before the scheduled start, up to
            fifteen minutes after the scheduled end, clamped to what was recorded.</li>
          <li>It goes onto that night's Class Recordings, where students look.</li>
          <li>A note goes to contact@aalb.org saying which class and how long.</li>
          <li>A night a trainer already posted to is never written over.</li>
        </ul>
      </div>

      <p className="text-sm text-gray-500 mt-6">
        Recordings post themselves automatically when a class is wired to its Zoom
        room. That screen is for the older ones, and for correcting a match.
      </p>
    </div>
  )
}
