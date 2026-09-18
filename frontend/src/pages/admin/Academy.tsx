/**
 * Add an old recording to a class on the academy.
 *
 * Every recording the archive holds, with the class night it looks like, and a
 * button to send the ones that match. Matching is on the session number in the
 * Zoom title: "Session 127" on a recording, "Session 127" on a class, and then
 * the night whose hours the recording actually overlaps.
 *
 * The page never decides anything. The academy works out the matches and does
 * the sending; this shows them and carries a person's yes. A match it got
 * wrong can be pointed at a different night before sending.
 *
 * Deliberately one at a time. Sending means cutting a three hour video and
 * uploading it, so a button that fired twenty of those at once would look
 * broken for an hour and there would be no saying which one failed.
 */
import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { academyApi, AcademyMatch, AcademyMatchState } from '../../services/api'

const STATE_WORDS: Record<AcademyMatchState, { label: string; tone: string; blurb: string }> = {
  ready: {
    label: 'Ready to add', tone: 'bg-green-100 text-green-800',
    blurb: 'Matched to a night, and nothing is posted there yet.'
  },
  posted: {
    label: 'Already added', tone: 'bg-blue-100 text-blue-800',
    blurb: 'This recording is already on that night.'
  },
  taken: {
    label: 'Night already has one', tone: 'bg-amber-100 text-amber-800',
    blurb: 'Somebody posted a recording there. It will not be written over.'
  },
  no_session: {
    label: 'No session in the title', tone: 'bg-gray-100 text-gray-700',
    blurb: 'The Zoom title does not say which session this is.'
  },
  no_class: {
    label: 'No class of that number', tone: 'bg-gray-100 text-gray-700',
    blurb: 'Nothing on the academy is named for that session.'
  },
  no_night: {
    label: 'No night matches', tone: 'bg-gray-100 text-gray-700',
    blurb: 'The class exists, but no night of it covers these hours.'
  },
  too_short: {
    label: 'Too little of the class', tone: 'bg-gray-100 text-gray-700',
    blurb: 'It overlaps the night, but holds too little of it to be worth posting.'
  },
  not_archived: {
    label: 'Still copying', tone: 'bg-gray-100 text-gray-700',
    blurb: 'Not safely in Drive yet. It can be added once that finishes.'
  }
}

const ORDER: AcademyMatchState[] = [
  'ready', 'not_archived', 'taken', 'posted',
  'no_night', 'too_short', 'no_class', 'no_session'
]

function when(iso?: string | null) {
  if (!iso) return 'unknown'
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'
  })
}

function gb(bytes?: number | null) {
  if (!bytes) return ''
  const g = bytes / 1e9
  return g >= 1 ? `${g.toFixed(1)} GB` : `${Math.round(bytes / 1e6)} MB`
}

export default function AcademyPage() {
  const queryClient = useQueryClient()
  const [onlyMatches, setOnlyMatches] = useState(true)
  const [session, setSession] = useState('')
  const [sending, setSending] = useState<string | null>(null)
  const [note, setNote] = useState<{ kind: 'ok' | 'bad'; text: string } | null>(null)

  const status = useQuery({ queryKey: ['academy-status'], queryFn: academyApi.status })
  const review = useQuery({
    queryKey: ['academy-review', session],
    queryFn: () => academyApi.review({ limit: 300, session: session || undefined }),
    enabled: status.data?.secret_configured !== false
  })

  const send = useMutation({
    mutationFn: academyApi.publish,
    onMutate: (v) => setSending(v.zoom_file_id),
    onSuccess: (res) => {
      setSending(null)
      setNote(res.outcome === 'posted'
        ? { kind: 'ok', text: `Added to ${res.row.cohort_name || 'the class'}.` }
        : { kind: 'bad', text: `The academy did not post it: ${res.row.why || res.outcome}` })
      queryClient.invalidateQueries({ queryKey: ['academy-review'] })
    },
    onError: (err: any) => {
      setSending(null)
      setNote({ kind: 'bad', text: err?.response?.data?.detail || err?.message || 'It did not go.' })
    }
  })

  const rows = review.data?.rows || []
  const shown = useMemo(() => {
    const keep = onlyMatches
      ? rows.filter((r) => r.state === 'ready' || r.state === 'not_archived')
      : rows
    return [...keep].sort((a, b) => {
      const byState = ORDER.indexOf(a.state) - ORDER.indexOf(b.state)
      if (byState !== 0) return byState
      return String(b.recording_start || '').localeCompare(String(a.recording_start || ''))
    })
  }, [rows, onlyMatches])

  if (status.data && !status.data.secret_configured) {
    return (
      <div className="bg-amber-50 border border-amber-200 rounded-lg p-6">
        <h1 className="text-xl font-semibold text-amber-900 mb-2">Not connected to the academy</h1>
        <p className="text-amber-800">
          This server has no academy secret, so it cannot add recordings to classes.
          Set <code className="font-mono text-sm">CLASS_BOT_SHARED_SECRET</code> here to the
          same value the academy uses, and this page will work.
        </p>
      </div>
    )
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Add recordings to the academy</h1>
        <p className="text-gray-600 mt-1">
          Matched by the session number in the Zoom title, then by which night the
          recording's hours cover. Sending cuts the class out and posts it to that
          night's Class Recordings, the same as one posted automatically.
        </p>
      </div>

      {note && (
        <div className={`mb-4 rounded-md p-3 text-sm ${
          note.kind === 'ok' ? 'bg-green-50 text-green-800' : 'bg-red-50 text-red-800'}`}>
          {note.text}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-4 mb-4">
        <label className="flex items-center gap-2 text-sm text-gray-700">
          <input type="checkbox" checked={onlyMatches}
            onChange={(e) => setOnlyMatches(e.target.checked)} />
          Only ones that match a class
        </label>
        <label className="flex items-center gap-2 text-sm text-gray-700">
          Session
          <input value={session} onChange={(e) => setSession(e.target.value.trim())}
            placeholder="all" className="w-20 border rounded px-2 py-1 text-sm" />
        </label>
        <button onClick={() => review.refetch()}
          className="text-sm text-blue-600 hover:text-blue-800">Refresh</button>
        {review.data && (
          <span className="text-sm text-gray-500">
            {Object.entries(review.data.counts)
              .map(([k, n]) => `${n} ${STATE_WORDS[k as AcademyMatchState]?.label.toLowerCase() || k}`)
              .join(' · ')}
          </span>
        )}
      </div>

      {review.isLoading && <p className="text-gray-500">Reading the archive...</p>}
      {review.isError && (
        <div className="bg-red-50 text-red-800 rounded-md p-4">
          Could not read the academy: {(review.error as any)?.response?.data?.detail
            || (review.error as any)?.message}
        </div>
      )}

      {review.data && shown.length === 0 && (
        <p className="text-gray-500">
          {onlyMatches
            ? 'Nothing is waiting to be added. Untick the box to see everything.'
            : 'The archive has no recordings yet.'}
        </p>
      )}

      <div className="space-y-3">
        {shown.map((row) => (
          <Row key={row.zoom_file_id} row={row}
            busy={sending === row.zoom_file_id}
            anyBusy={sending !== null}
            onSend={() => row.cohort_day_id && send.mutate({
              zoom_file_id: row.zoom_file_id, cohort_day_id: row.cohort_day_id
            })} />
        ))}
      </div>
    </div>
  )
}

function Row({ row, busy, anyBusy, onSend }: {
  row: AcademyMatch; busy: boolean; anyBusy: boolean; onSend: () => void
}) {
  const words = STATE_WORDS[row.state]
  return (
    <div className="bg-white border border-gray-200 rounded-lg p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${words.tone}`}>
              {words.label}
            </span>
            {row.session_number && (
              <span className="text-xs font-semibold text-gray-500">
                Session {row.session_number}
              </span>
            )}
          </div>

          <p className="font-medium text-gray-900 truncate mt-1">{row.topic || 'Untitled'}</p>
          <p className="text-sm text-gray-600">
            Recorded {when(row.recording_start)}
            {row.size_bytes ? ` · ${gb(row.size_bytes)}` : ''}
          </p>

          {row.cohort_name && (
            <p className="text-sm text-gray-800 mt-2">
              {row.cohort_name}
              {row.day_number != null && <> · <b>Day {row.day_number}</b></>}
              {row.day_date && <> · {row.day_date}</>}
              {row.overlap_minutes != null && (
                <span className="text-gray-500"> · covers {row.overlap_minutes} min of that night</span>
              )}
            </p>
          )}

          {row.cut && (
            <p className="text-sm text-gray-500">
              Would post <b className="text-gray-800">{row.cut.length}</b> as
              {' '}&ldquo;{row.would_be_titled}&rdquo;
            </p>
          )}

          <p className="text-sm text-gray-500 mt-1">{row.why || words.blurb}</p>

          {row.drive_url && (
            <a href={row.drive_url} target="_blank" rel="noopener noreferrer"
              className="text-sm text-blue-600 hover:text-blue-800">Open what was posted</a>
          )}
        </div>

        <div className="shrink-0">
          {row.state === 'ready' ? (
            <button onClick={onSend} disabled={anyBusy}
              className="px-4 py-2 rounded-md text-sm font-medium bg-blue-600 text-white
                         hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed">
              {busy ? 'Adding...' : 'Add to class'}
            </button>
          ) : (
            <span className="text-sm text-gray-400">
              {row.state === 'posted' ? 'Done' : 'Nothing to do'}
            </span>
          )}
        </div>
      </div>

      {busy && (
        <p className="text-sm text-gray-500 mt-3">
          Cutting the class out and uploading it. A three hour recording takes a
          few minutes, and this page waits for it.
        </p>
      )}
    </div>
  )
}
