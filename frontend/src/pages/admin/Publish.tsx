import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  publishApi,
  ClassSettings,
  PublishPlan,
  PublishJobStatus,
} from '../../services/api'

/**
 * Publish — Zoom recordings, trimmed, into Drive and Google Classroom.
 *
 * Three screens, one job each:
 *   list      scan the week, send the ones that are fine as-is
 *   review    adjust anything about one recording, then send it
 *   settings  per-class defaults, so review only ever asks you to confirm
 *
 * The old Trim & Upload tab is untouched and still works.
 */

// Class colors. Every value clears 7:1 on white so it can carry text.
const COLORS: Record<string, { ink: string; soft: string; border: string }> = {
  teal: { ink: '#0A4F4B', soft: '#E3EFEE', border: '#0A4F4B' },
  blue: { ink: '#10457E', soft: '#E4ECF6', border: '#10457E' },
  plum: { ink: '#6B2A5B', soft: '#F2E7EF', border: '#6B2A5B' },
  amber: { ink: '#663D03', soft: '#FBF0DC', border: '#663D03' },
  green: { ink: '#0E5533', soft: '#E2F0E8', border: '#0E5533' },
}
const color = (name: string) => COLORS[name] || COLORS.teal

const ACCENT = '#0A4F4B'
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

// Every trim, day number and date is computed in the class's own timezone,
// so it needs to be visible and changeable rather than assumed.
const TIMEZONES = [
  ['America/New_York', 'Eastern (New York)'],
  ['America/Chicago', 'Central (Chicago)'],
  ['America/Denver', 'Mountain (Denver)'],
  ['America/Phoenix', 'Arizona (no DST)'],
  ['America/Los_Angeles', 'Pacific (Los Angeles)'],
  ['America/Anchorage', 'Alaska'],
  ['Pacific/Honolulu', 'Hawaii'],
  ['America/Puerto_Rico', 'Puerto Rico'],
  ['America/Santo_Domingo', 'Dominican Republic'],
  ['Europe/London', 'UK'],
  ['UTC', 'UTC'],
]

const hm = (s: number) => {
  const t = Math.round(s / 60)
  const h = Math.floor(t / 60)
  return h ? `${h}h ${t % 60}m` : `${t} min`
}

const tc = (s: number) => {
  const v = Math.max(0, Math.round(s))
  return `${Math.floor(v / 3600)}:${String(Math.floor((v % 3600) / 60)).padStart(2, '0')}:${String(v % 60).padStart(2, '0')}`
}

const parseTc = (value: string) => {
  const parts = value.trim().split(':').map((p) => parseInt(p, 10) || 0)
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2]
  if (parts.length === 2) return parts[0] * 60 + parts[1]
  return parts[0] || 0
}

const gb = (bytes: number) =>
  bytes >= 1e9 ? `${(bytes / 1e9).toFixed(2)} GB` : `${Math.round(bytes / 1e6)} MB`

const BLOCKER_TEXT: Record<string, string> = {
  no_session_code: "Zoom didn't put a session number in the title, so this isn't matched to a class yet.",
  class_not_configured: 'This session has no settings yet — add the class to tell it where recordings go.',
  no_day_number: "This date isn't a scheduled class day, so we don't know which day number to use.",
  no_video_files: 'Zoom has no video files for this recording yet. It may still be processing.',
}

export default function PublishPage() {
  const queryClient = useQueryClient()
  const [view, setView] = useState<'list' | 'review' | 'settings'>('list')
  const [active, setActive] = useState<PublishPlan | null>(null)

  const { data, isLoading, error } = useQuery({
    queryKey: ['publish-queue'],
    queryFn: () => publishApi.queue(14),
    refetchOnWindowFocus: false,
  })

  const openReview = (plan: PublishPlan) => {
    setActive(plan)
    setView('review')
    window.scrollTo(0, 0)
  }

  const backToList = () => {
    setActive(null)
    setView('list')
    queryClient.invalidateQueries({ queryKey: ['publish-queue'] })
    window.scrollTo(0, 0)
  }

  return (
    <div className="max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-gray-900">Class recordings</h1>
        <button
          onClick={() => setView(view === 'settings' ? 'list' : 'settings')}
          className="text-sm font-medium underline underline-offset-4"
          style={{ color: ACCENT }}
        >
          {view === 'settings' ? 'Back to recordings' : 'Class settings'}
        </button>
      </div>

      {view === 'settings' && <Settings onDone={backToList} />}

      {view === 'review' && active && (
        <Review plan={active} onBack={backToList} />
      )}

      {view === 'list' && (
        <>
          {isLoading && <p className="text-gray-500 py-10 text-center">Loading recordings…</p>}
          {error && (
            <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
              Couldn't load recordings: {(error as Error).message}
            </div>
          )}
          {data && (
            <>
              {data.classes_configured === 0 && (
                <Callout tone="amber">
                  No classes are set up yet. Open <b>Class settings</b> to add one — then every
                  recording of that class arrives already filled in.
                </Callout>
              )}
              {!data.classroom_configured && data.classes_configured > 0 && (
                <Callout tone="amber">
                  Google Classroom isn't connected, so recordings will upload to Drive and give you
                  a link to post by hand. Add a teacher email in <b>Class settings</b> to post
                  automatically.
                </Callout>
              )}

              <Group
                title="Not matched to a class"
                dot={COLORS.amber.ink}
                plans={data.recordings.filter((r) => r.state === 'needs_attention')}
                onOpen={openReview}
              />
              <Group
                title="Ready to send"
                dot={COLORS.teal.ink}
                plans={data.recordings.filter((r) => r.state === 'ready')}
                onOpen={openReview}
              />
              <Group
                title="Published"
                dot={COLORS.green.ink}
                plans={data.recordings.filter((r) => r.state === 'published')}
                onOpen={openReview}
              />

              {data.recordings.length === 0 && (
                <p className="text-center text-gray-500 py-12">
                  No Zoom recordings in the last 14 days.
                </p>
              )}
            </>
          )}
        </>
      )}
    </div>
  )
}

function Callout({ tone, children }: { tone: 'amber' | 'teal'; children: React.ReactNode }) {
  const c = tone === 'amber' ? COLORS.amber : COLORS.teal
  return (
    <div
      className="rounded-lg border p-4 text-sm mb-5"
      style={{ background: c.soft, borderColor: c.ink, color: c.ink }}
    >
      {children}
    </div>
  )
}

function Group({
  title,
  dot,
  plans,
  onOpen,
}: {
  title: string
  dot: string
  plans: PublishPlan[]
  onOpen: (p: PublishPlan) => void
}) {
  if (plans.length === 0) return null
  return (
    <section className="mb-8">
      <div className="flex items-center justify-between border-b border-gray-200 pb-2 mb-3">
        <h2 className="flex items-center gap-2 text-xs font-bold uppercase tracking-wide text-gray-600">
          <span className="w-2.5 h-2.5 rounded-full" style={{ background: dot }} />
          {title}
        </h2>
        <span className="text-sm text-gray-500">
          {plans.length} recording{plans.length === 1 ? '' : 's'}
        </span>
      </div>
      <div className="space-y-2">
        {plans.map((p) => (
          <Row key={p.recording_id} plan={p} onOpen={onOpen} />
        ))}
      </div>
    </section>
  )
}

function Row({ plan, onOpen }: { plan: PublishPlan; onOpen: (p: PublishPlan) => void }) {
  const c = color(plan.class_color)
  const kept = plan.trim.end_seconds - plan.trim.start_seconds
  const state = plan.state
  // Under 10 minutes is almost never a real class — worth calling out rather
  // than making someone open it to find out.
  const short = plan.duration_seconds > 0 && plan.duration_seconds < 600

  const pill =
    state === 'published'
      ? { text: 'Published', ...COLORS.green }
      : state === 'ready'
      ? { text: 'Ready', ...COLORS.teal }
      : { text: 'Unmatched', ...COLORS.amber }

  return (
    <div
      className="bg-white rounded-lg border border-gray-200 p-4 grid gap-4 items-center"
      style={{ borderLeft: `5px solid ${c.border}`, gridTemplateColumns: 'auto 1fr auto' }}
    >
      <div
        className="w-12 h-12 rounded-lg grid place-content-center text-center leading-none"
        style={{ background: c.soft, color: c.ink }}
      >
        {plan.session_code ? (
          <>
            <b className="block text-lg font-bold tabular-nums">{plan.session_code}</b>
            <span className="block text-[8px] font-bold tracking-widest uppercase opacity-90">
              Session
            </span>
          </>
        ) : (
          <span className="text-xl font-bold">?</span>
        )}
      </div>

      <div className="min-w-0">
        <div className="text-xs font-semibold uppercase tracking-wide text-gray-500">
          {plan.date_label}
          {plan.day_number != null && ` · Day ${plan.day_number}`}
        </div>
        <p className="font-semibold text-gray-900 truncate">{plan.class_label || plan.topic}</p>

        {/* Length and size show on every row, matched or not — it's the only
            way to tell a real class from a one-minute misfire at a glance. */}
        <p className="text-sm text-gray-600 mt-0.5">
          <b className={short ? '' : 'text-gray-900'} style={short ? { color: COLORS.amber.ink } : undefined}>
            {hm(plan.duration_seconds)}
          </b>{' '}
          recorded
          {plan.trim.source === 'schedule' && (
            <> · <b className="text-gray-900">{hm(kept)}</b> after trim</>
          )}
          {' · '}
          {plan.available_views.length} video{plan.available_views.length === 1 ? '' : 's'}
          {plan.total_size_bytes > 0 && <> · {gb(plan.total_size_bytes)}</>}
          {short && (
            <span style={{ color: COLORS.amber.ink }}> · very short, probably not a class</span>
          )}
          {state === 'published' && plan.published?.classroom && !plan.published.classroom.ok && (
            <span style={{ color: COLORS.amber.ink }}> · Drive only, not in Classroom</span>
          )}
        </p>

        {state === 'needs_attention' && (
          <p className="text-sm mt-0.5" style={{ color: COLORS.amber.ink }}>
            {BLOCKER_TEXT[plan.blockers[0]] || 'Not matched to a class.'}
          </p>
        )}
      </div>

      <div className="flex items-center gap-3">
        <span
          className="text-[11px] font-bold uppercase tracking-wide px-2.5 py-1 rounded-full whitespace-nowrap"
          style={{ background: pill.soft, color: pill.ink }}
        >
          {pill.text}
        </span>
        <button
          onClick={() => onOpen(plan)}
          className="px-4 py-2 rounded-lg text-sm font-semibold text-white"
          style={{ background: ACCENT }}
        >
          {state === 'published' ? 'Details' : 'Review'}
        </button>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ review */

function Review({ plan, onBack }: { plan: PublishPlan; onBack: () => void }) {
  const [draft, setDraft] = useState(plan)
  const [start, setStart] = useState(plan.trim.start_seconds)
  const [end, setEnd] = useState(plan.trim.end_seconds)
  const [selected, setSelected] = useState<string[]>(plan.outputs.map((o) => o.key))
  const [title, setTitle] = useState(plan.title)
  const [note, setNote] = useState('')
  const [postState, setPostState] = useState(plan.post_state || 'PUBLISHED')
  const [manualStart, setManualStart] = useState<string | null>(null)
  const [manualDuration, setManualDuration] = useState<number>(180)
  const [jobId, setJobId] = useState<string | null>(null)
  const [job, setJob] = useState<PublishJobStatus | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const outcomeRef = useRef<HTMLDivElement | null>(null)

  const { data: settings } = useQuery({
    queryKey: ['publish-settings'],
    queryFn: publishApi.settings,
  })

  const readOnly = draft.state === 'published' && !jobId
  const cls = settings?.classes.find((c) => c.code === draft.session_code)

  // Bring the result into view the moment it lands.
  useEffect(() => {
    if (job?.status === 'completed') {
      outcomeRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, [job?.status])

  // poll while a job runs
  useEffect(() => {
    if (!jobId || job?.status === 'completed' || job?.status === 'failed') return
    const timer = setInterval(async () => {
      try {
        setJob(await publishApi.status(jobId))
      } catch {
        /* keep polling; a transient error shouldn't kill the view */
      }
    }, 2000)
    return () => clearInterval(timer)
  }, [jobId, job?.status])

  /**
   * Re-ask the backend for the plan with whatever the user changed. Class, day
   * and a hand-typed start time all feed the same call, so the title, filenames
   * and trim stay consistent with each other instead of drifting.
   */
  const replan = async (patch: {
    sessionCode?: string | null
    dayNumber?: number | null
    manualStart?: string | null
    manualDuration?: number | null
  }) => {
    const replanned = await publishApi.replan(
      {
        id: draft.recording_id,
        meeting_id: draft.meeting_id,
        topic: draft.topic,
        start_time: draft.start_time,
        duration: draft.duration_seconds / 60,
        host_name: draft.host_name,
        recording_files: draft.available_views.map((v) => ({
          id: v.file_id,
          file_type: 'MP4',
          file_size: v.size_bytes,
          download_url: v.download_url,
          recording_type: v.zoom_type,
        })),
      },
      patch.sessionCode !== undefined ? patch.sessionCode : draft.session_code,
      patch.dayNumber !== undefined ? patch.dayNumber : draft.day_number,
      patch.manualStart !== undefined ? patch.manualStart : manualStart,
      patch.manualDuration !== undefined ? patch.manualDuration : manualDuration,
    )
    setDraft({ ...replanned, state: replanned.ready ? 'ready' : 'needs_attention', published: null })
    setStart(replanned.trim.start_seconds)
    setEnd(replanned.trim.end_seconds)
    setSelected(replanned.outputs.map((o) => o.key))
    setTitle(replanned.title)
  }

  const send = async () => {
    setFailed(null)
    const outputs = draft.available_views
      .filter((v) => selected.includes(v.key))
      .map((v) => ({
        key: v.key,
        folder: v.folder || v.key,
        download_url: v.download_url,
        filename: v.filename,
        drive_folders: v.drive_folders,
      }))
    try {
      const res = await publishApi.start({
        recording_id: draft.recording_id,
        session_code: draft.session_code || '',
        day_number: draft.day_number,
        date_key: draft.date_key,
        title,
        description: note,
        outputs,
        start_seconds: start,
        end_seconds: end,
        course_id: draft.course_id,
        topic_id: draft.topic_id,
        post_state: postState,
      })
      setJobId(res.job_id)
      setJob({ job_id: res.job_id, status: 'pending', progress: 0, message: 'Starting…' })
    } catch (e: any) {
      setFailed(e?.response?.data?.detail || e.message || 'Could not start the job.')
    }
  }

  const busy = !!jobId && job?.status !== 'completed' && job?.status !== 'failed'
  const kept = end - start
  // Classroom only happens when this recording resolves to a course. Without
  // one it's still a perfectly good Drive upload.
  const willPostToClassroom = !!draft.course_id

  return (
    <div>
      <button onClick={onBack} className="text-sm font-medium mb-4 underline underline-offset-4" style={{ color: ACCENT }}>
        ‹ All recordings
      </button>

      <h2 className="text-xl font-bold text-gray-900">{draft.class_label || draft.topic}</h2>
      <p className="text-sm text-gray-600 mt-1 mb-5">
        {draft.date_label} · {hm(draft.duration_seconds)} recorded · hosted by {draft.host_name}
        {draft.day_number != null && ` · Day ${draft.day_number}`}
      </p>

      {/* Result sits at the top: after sending, the link is the thing you want,
          and it shouldn't be below three screens of settings. */}
      {(job?.status === 'completed' || draft.published) && (
        <div ref={outcomeRef}>
          <Outcome result={job?.result || draft.published} />
        </div>
      )}

      {/* Class and day: always shown, always editable. Auto-detected when we
          can, but never assumed — the day ends up in the title and filename. */}
      <Card title="Class and day" tone={draft.session_code ? 'teal' : 'amber'}>
        {!draft.ready && draft.blockers.length > 0 && (
          <p className="text-sm mb-3" style={{ color: COLORS.amber.ink }}>
            {BLOCKER_TEXT[draft.blockers[0]]}
          </p>
        )}
        <p className="text-sm text-gray-600 mb-4">
          The date is always in the title. Set the day number too and it goes in as
          <span className="font-mono text-xs"> Day N</span>. Leave the class blank and it still
          uploads to Drive under <span className="font-mono text-xs">{draft.drive_root}/</span>.
        </p>

        <div className="grid sm:grid-cols-3 gap-4">
          <label className="block">
            <span className="block text-sm font-semibold mb-1">Class</span>
            <select
              className="input"
              value={draft.session_code || ''}
              disabled={readOnly}
              onChange={(e) => replan({ sessionCode: e.target.value || null })}
            >
              <option value="">No class — Drive only</option>
              {settings?.classes.map((c) => (
                <option key={c.code} value={c.code}>{c.label}</option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="block text-sm font-semibold mb-1">
              Class day
              {draft.day_number != null && !readOnly && (
                <span className="font-normal text-xs ml-1" style={{ color: COLORS.green.ink }}>
                  auto-detected
                </span>
              )}
            </span>
            <input
              type="number"
              min={1}
              className="input"
              placeholder="e.g. 5"
              defaultValue={draft.day_number ?? ''}
              disabled={readOnly}
              key={`day-${draft.day_number}`}
              onBlur={(e) =>
                replan({ dayNumber: e.target.value ? parseInt(e.target.value, 10) : null })
              }
            />
          </label>

          <div>
            <span className="block text-sm font-semibold mb-1">Recording date</span>
            <div className="input bg-gray-50 text-gray-700">{draft.date_label}</div>
            <p className="hint text-xs text-gray-500 mt-1">Always included in the title.</p>
          </div>
        </div>

        <div className="mt-4 rounded-lg border border-gray-200 bg-gray-50 p-3">
          <span className="block text-sm font-semibold mb-1">Title students will see</span>
          <p className="font-mono text-xs text-gray-800 break-words">{title}</p>
        </div>
      </Card>

      {/* When there's no schedule to trim against, ask for the start time. */}
      {draft.trim.source !== 'schedule' && (
        <Card title="When did the class start?" tone="amber">
          <p className="text-sm text-gray-600 mb-4">
            There's no schedule to trim against, so tell us when class actually started. We'll keep
            5 minutes before it and 5 minutes after it ends. Recording began at{' '}
            <b className="font-mono">{draft.started_local}</b>.
          </p>
          <div className="grid sm:grid-cols-3 gap-4 items-end">
            <label className="block">
              <span className="block text-sm font-semibold mb-1">Class started at</span>
              <input
                type="time"
                className="input"
                value={manualStart ?? ''}
                disabled={readOnly}
                onChange={(e) => setManualStart(e.target.value || null)}
              />
            </label>
            <label className="block">
              <span className="block text-sm font-semibold mb-1">Class length</span>
              <select
                className="input"
                value={manualDuration}
                disabled={readOnly}
                onChange={(e) => setManualDuration(parseInt(e.target.value, 10))}
              >
                {[60, 90, 120, 150, 180, 210, 240].map((m) => (
                  <option key={m} value={m}>
                    {Math.floor(m / 60)}h{m % 60 ? ` ${m % 60}m` : ''}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              disabled={!manualStart || readOnly}
              onClick={() => replan({ manualStart, manualDuration })}
              className="px-4 py-2 rounded-lg text-sm font-semibold text-white disabled:opacity-50"
              style={{ background: ACCENT }}
            >
              Apply trim
            </button>
          </div>
        </Card>
      )}

      {false && (
        <Card title="Which class is this? (optional)" tone="amber">
          <p className="text-sm text-gray-600 mb-2">
            {BLOCKER_TEXT[draft.blockers[0]]}
          </p>
          <p className="text-sm text-gray-600 mb-4">
            You can skip this and send it anyway — it'll upload to Drive under{' '}
            <span className="font-mono text-xs">{draft.drive_root}/</span> keeping its Zoom title.
            Matching it to a class only adds the day number, the tidy filename, and the
            Classroom post.
          </p>
          <div className="grid sm:grid-cols-2 gap-4">
            <label className="block">
              <span className="block text-sm font-semibold mb-1">Class</span>
              <select
                className="input"
                defaultValue={draft.session_code || ''}
                onChange={(e) => e.target.value && replan({ sessionCode: e.target.value })}
              >
                <option value="">Choose a class…</option>
                {settings?.classes.map((c) => (
                  <option key={c.code} value={c.code}>{c.label}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="block text-sm font-semibold mb-1">Class day</span>
              <input
                type="number"
                min={1}
                className="input"
                defaultValue={draft.day_number ?? ''}
                onBlur={(e) =>
                  replan({ dayNumber: e.target.value ? parseInt(e.target.value, 10) : null })
                }
              />
            </label>
          </div>
          {settings?.classes.length === 0 && (
            <p className="text-sm mt-3" style={{ color: COLORS.amber.ink }}>
              No classes exist yet — add one in Class settings first.
            </p>
          )}
        </Card>
      )}

      <Card title="Trim" tone="teal">
        <p className="text-sm text-gray-600 mb-4">{draft.trim.note}</p>
        <Timeline
          duration={draft.duration_seconds}
          start={start}
          end={end}
          disabled={readOnly}
          onChange={(s, e) => {
            setStart(s)
            setEnd(e)
          }}
        />
        <div className="flex flex-wrap gap-6 mt-5">
          <TimeField label="Starts at" value={start} disabled={readOnly}
            onChange={(v) => setStart(Math.max(0, Math.min(v, end - 60)))} />
          <TimeField label="Ends at" value={end} disabled={readOnly}
            onChange={(v) => setEnd(Math.min(draft.duration_seconds, Math.max(v, start + 60)))} />
          <div className="self-end pb-2 text-sm text-gray-600">
            Sends <b className="text-gray-900 tabular-nums">{hm(kept)}</b> · trims{' '}
            <b className="text-gray-900">{hm(start)}</b> from the start,{' '}
            <b className="text-gray-900">{hm(draft.duration_seconds - end)}</b> from the end
          </div>
        </div>
      </Card>

      <Card title="Which videos to send" tone="blue">
        {draft.available_views.length === 0 && (
          <p className="text-sm text-gray-600">Zoom has no video files for this recording yet.</p>
        )}
        {draft.available_views.map((v) => {
          const on = selected.includes(v.key)
          return (
            <label
              key={v.key}
              className="flex items-center gap-3 p-3 rounded-lg border mb-2 cursor-pointer"
              style={{
                borderColor: on ? COLORS.blue.ink : '#E5E7EB',
                background: on ? COLORS.blue.soft : '#fff',
              }}
            >
              <input
                type="checkbox"
                checked={on}
                disabled={readOnly}
                onChange={(e) =>
                  setSelected(
                    e.target.checked
                      ? [...selected, v.key]
                      : selected.filter((k) => k !== v.key),
                  )
                }
                className="w-4 h-4"
              />
              <span className="flex-1 min-w-0">
                <b className="block text-sm font-semibold">{v.name}</b>
                <span className="block text-xs text-gray-500">{v.description}</span>
              </span>
              <span className="text-sm text-gray-600 tabular-nums">{gb(v.size_bytes)}</span>
            </label>
          )
        })}
      </Card>

      <Card title="Where it goes" tone="plum">
        <label className="block mb-4">
          <span className="block text-sm font-semibold mb-1">Title students see</span>
          <input className="input" value={title} disabled={readOnly}
            onChange={(e) => setTitle(e.target.value)} />
        </label>

        <label className="block mb-4">
          <span className="block text-sm font-semibold mb-1">
            Note to students <span className="font-normal text-gray-500">(optional)</span>
          </span>
          <textarea className="input" rows={2} value={note} disabled={readOnly}
            onChange={(e) => setNote(e.target.value)} />
        </label>

        <div className="mb-4">
          <span className="block text-sm font-semibold mb-1">When students see it</span>
          <div className="flex gap-2 flex-wrap">
            {[
              { v: 'PUBLISHED', label: 'Post immediately' },
              { v: 'DRAFT', label: 'Save as draft' },
            ].map((o) => (
              <label
                key={o.v}
                className="flex items-center gap-2 px-3 py-2 rounded-lg border text-sm cursor-pointer"
                style={{
                  borderColor: postState === o.v ? ACCENT : '#D1D5DB',
                  background: postState === o.v ? COLORS.teal.soft : '#fff',
                  color: postState === o.v ? ACCENT : undefined,
                }}
              >
                <input type="radio" checked={postState === o.v} disabled={readOnly}
                  onChange={() => setPostState(o.v)} />
                {o.label}
              </label>
            ))}
          </div>
        </div>

        <div className="rounded-lg bg-gray-50 border border-gray-200 p-3 text-xs font-mono text-gray-800 space-y-1">
          {draft.available_views
            .filter((v) => selected.includes(v.key))
            .map((v) => (
              <div key={v.key}>
                <span className="text-gray-500">
                  Drive / {(v.drive_folders || []).join(' / ')} /{' '}
                </span>
                {v.filename}
              </div>
            ))}
          <div className="pt-1">
            <span className="text-gray-500">Classroom: </span>
            {draft.course_name || cls?.classroom_course_name || 'not posting — you\'ll get the Drive link to attach by hand'}
            {draft.topic_name && <span className="text-gray-500"> → {draft.topic_name}</span>}
          </div>
        </div>
      </Card>

      {/* action bar */}
      <div className="sticky bottom-4 bg-white border border-gray-200 rounded-xl p-4 flex items-center gap-4 flex-wrap shadow-lg">
        <span className="text-sm text-gray-600">
          Sending <b className="text-gray-900">{selected.length}</b> video
          {selected.length === 1 ? '' : 's'} · <b className="text-gray-900">{hm(kept)}</b> each ·{' '}
          {willPostToClassroom ? 'Drive + Classroom' : 'Drive only'}
        </span>
        <span className="flex-1" />

        {job && (
          <div className="min-w-[220px]">
            <p className="text-sm text-gray-700 mb-1">{job.message}</p>
            <div className="h-1.5 rounded-full bg-gray-200 overflow-hidden">
              <div className="h-full rounded-full transition-all"
                style={{ width: `${Math.round(job.progress * 100)}%`, background: ACCENT }} />
            </div>
          </div>
        )}

        {!readOnly && (
          <button
            onClick={send}
            disabled={busy || selected.length === 0}
            className="px-5 py-2.5 rounded-lg text-sm font-semibold text-white disabled:opacity-50"
            style={{ background: ACCENT }}
          >
            {busy
              ? 'Sending…'
              : willPostToClassroom
              ? 'Send to Classroom'
              : 'Upload to Drive'}
          </button>
        )}
      </div>

      {failed && (
        <div className="mt-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
          {failed}
        </div>
      )}

      {job?.status === 'failed' && (
        <div className="mt-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">
          <b>Publish failed.</b> {job.error || job.message}
        </div>
      )}
    </div>
  )
}

function Outcome({ result }: { result: any }) {
  if (!result) return null
  const classroom = result.classroom
  const files = result.files || []
  return (
    <div className="mb-5 rounded-lg border p-4"
      style={{ borderColor: COLORS.green.ink, background: COLORS.green.soft }}>
      <b className="block mb-2" style={{ color: COLORS.green.ink }}>
        {classroom?.ok ? 'Published to Classroom' : 'Uploaded to Drive'}
      </b>
      <ul className="text-sm space-y-1">
        {files.map((f: any) => (
          <li key={f.file_id}>
            <a href={f.link} target="_blank" rel="noopener noreferrer"
              className="underline underline-offset-2" style={{ color: ACCENT }}>
              {f.name}
            </a>
          </li>
        ))}
      </ul>
      {classroom?.ok && classroom.link && (
        <a href={classroom.link} target="_blank" rel="noopener noreferrer"
          className="inline-block mt-3 text-sm font-semibold underline underline-offset-2"
          style={{ color: ACCENT }}>
          Open the Classroom post →
        </a>
      )}
      {classroom && !classroom.ok && (
        <div className="mt-3 rounded-lg border p-3 text-sm"
          style={{ borderColor: COLORS.amber.ink, background: '#fff', color: COLORS.amber.ink }}>
          <b>Not posted to Classroom.</b> {classroom.detail}
          <div className="mt-1 text-gray-700">
            The video is safely in Drive — use the link above to attach it by hand.
          </div>
        </div>
      )}
    </div>
  )
}

function Card({
  title,
  tone,
  children,
}: {
  title: string
  tone: keyof typeof COLORS
  children: React.ReactNode
}) {
  const c = COLORS[tone]
  return (
    <section className="bg-white rounded-xl border border-gray-200 p-5 mb-4">
      <h3 className="flex items-center gap-2.5 text-base font-semibold mb-3">
        <span className="w-7 h-7 rounded-lg grid place-content-center text-xs font-bold"
          style={{ background: c.soft, color: c.ink }}>
          ●
        </span>
        {title}
      </h3>
      {children}
    </section>
  )
}

function TimeField({
  label,
  value,
  disabled,
  onChange,
}: {
  label: string
  value: number
  disabled?: boolean
  onChange: (v: number) => void
}) {
  const [text, setText] = useState(tc(value))
  useEffect(() => setText(tc(value)), [value])
  return (
    <div>
      <span className="block text-sm font-semibold mb-1">{label}</span>
      <div className="flex items-center gap-1.5">
        <button type="button" disabled={disabled} onClick={() => onChange(value - 10)}
          className="w-9 h-9 rounded-lg border border-gray-300 font-mono disabled:opacity-40">−</button>
        <input
          className="input font-mono tabular-nums w-32"
          value={text}
          disabled={disabled}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => onChange(parseTc(text))}
        />
        <button type="button" disabled={disabled} onClick={() => onChange(value + 10)}
          className="w-9 h-9 rounded-lg border border-gray-300 font-mono disabled:opacity-40">+</button>
      </div>
    </div>
  )
}

/** Simple timeline with two draggable handles. No fake thumbnails. */
function Timeline({
  duration,
  start,
  end,
  disabled,
  onChange,
}: {
  duration: number
  start: number
  end: number
  disabled?: boolean
  onChange: (start: number, end: number) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const drag = (which: 'start' | 'end') => (e: React.PointerEvent) => {
    if (disabled || !ref.current) return
    e.preventDefault()
    const box = ref.current.getBoundingClientRect()
    const move = (ev: PointerEvent) => {
      const frac = Math.min(1, Math.max(0, (ev.clientX - box.left) / box.width))
      const t = Math.round(frac * duration)
      if (which === 'start') onChange(Math.min(t, end - 60), end)
      else onChange(start, Math.max(t, start + 60))
    }
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  const pct = (v: number) => `${duration ? (v / duration) * 100 : 0}%`

  return (
    <>
      <div ref={ref} className="relative h-16 rounded-lg bg-gray-100 border border-gray-300 select-none">
        <div className="absolute inset-y-0 rounded-l-lg bg-gray-200" style={{ left: 0, width: pct(start) }} />
        <div className="absolute inset-y-0" style={{ left: pct(start), width: pct(end - start), background: COLORS.teal.soft }} />
        <div className="absolute inset-y-0 rounded-r-lg bg-gray-200" style={{ left: pct(end), right: 0 }} />
        {(['start', 'end'] as const).map((which) => (
          <button
            key={which}
            onPointerDown={drag(which)}
            disabled={disabled}
            aria-label={which === 'start' ? 'Trim start' : 'Trim end'}
            className="absolute top-0 bottom-0 w-6 -ml-3 grid place-items-center cursor-ew-resize disabled:cursor-default"
            style={{ left: pct(which === 'start' ? start : end) }}
          >
            <span className="w-1 h-full absolute" style={{ background: ACCENT }} />
            <span className="relative w-3 h-9 rounded" style={{ background: ACCENT }} />
            <span className="absolute top-1 text-[9px] font-bold uppercase px-1.5 py-0.5 rounded text-white"
              style={{ background: ACCENT }}>
              {which === 'start' ? 'Start' : 'End'}
            </span>
          </button>
        ))}
      </div>
      <div className="flex justify-between text-xs text-gray-500 font-mono mt-1.5">
        <span>0:00:00</span>
        <span>{tc(duration)}</span>
      </div>
    </>
  )
}

/* ---------------------------------------------------------------- settings */

function Settings({ onDone }: { onDone: () => void }) {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({ queryKey: ['publish-settings'], queryFn: publishApi.settings })
  const { data: courses } = useQuery({ queryKey: ['classroom-courses'], queryFn: publishApi.courses })

  const [subject, setSubject] = useState('')
  const [webhook, setWebhook] = useState('')
  const [timezone, setTimezone] = useState('America/New_York')
  const [saved, setSaved] = useState(false)
  // Every hook must run on every render — declaring this below the `isLoading`
  // early return changed the hook count between renders and blanked the page.
  const [adding, setAdding] = useState<ClassSettings | null>(null)

  useEffect(() => {
    if (data) {
      setSubject(data.classroom_subject)
      setWebhook(data.webhook_url)
      setTimezone(data.default_timezone || 'America/New_York')
    }
  }, [data])

  const viewKeys = useMemo(() => Object.keys(data?.view_types || {}), [data])

  if (isLoading) return <p className="text-gray-500 py-10 text-center">Loading settings…</p>

  const saveTop = async () => {
    await publishApi.saveSettings({
      classroom_subject: subject,
      webhook_url: webhook,
      default_timezone: timezone,
    })
    setSaved(true)
    queryClient.invalidateQueries({ queryKey: ['publish-settings'] })
    queryClient.invalidateQueries({ queryKey: ['classroom-courses'] })
    setTimeout(() => setSaved(false), 2500)
  }

  const blank = (): ClassSettings => ({
    code: '',
    label: '',
    color: data?.palette[(data?.classes.length || 0) % (data?.palette.length || 1)] || 'teal',
    timezone: 'America/New_York',
    scheduled_start: '',
    scheduled_end: '',
    meeting_weekdays: [],
    first_class_date: '',
    pad_before_minutes: 1,
    pad_after_minutes: 5,
    views: ['speaker'],
    filename_pattern: 'Session {session} - Day {day} - {date} ({view}).mp4',
    title_pattern: '{course} — Day {day} ({date})',
    drive_folder_id: '',
    classroom_course_id: '',
    classroom_course_name: '',
    classroom_topic_id: '',
    classroom_topic_name: '',
    post_state: 'PUBLISHED',
    share_mode: 'VIEW',
  })

  return (
    <div>
      <section className="bg-white rounded-xl border border-gray-200 p-5 mb-4">
        <h3 className="text-base font-semibold mb-1">Google Classroom</h3>
        <p className="text-sm text-gray-600 mb-4">
          The teacher account the app posts as. Leave blank to upload to Drive only and post by hand.
        </p>
        <div className="grid sm:grid-cols-2 gap-4">
          <label className="block">
            <span className="block text-sm font-semibold mb-1">Teacher email</span>
            <input className="input" value={subject} placeholder="teacher@aalb.org"
              onChange={(e) => setSubject(e.target.value)} />
          </label>
          <label className="block">
            <span className="block text-sm font-semibold mb-1">
              Send results to your own service <span className="font-normal text-gray-500">(optional)</span>
            </span>
            <input className="input" value={webhook} placeholder="https://…"
              onChange={(e) => setWebhook(e.target.value)} />
          </label>
          <label className="block">
            <span className="block text-sm font-semibold mb-1">Default time zone</span>
            <select className="input" value={timezone} onChange={(e) => setTimezone(e.target.value)}>
              {TIMEZONES.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
            </select>
            <span className="block text-xs text-gray-500 mt-1">
              Used for recordings that aren't matched to a class. Each class can override it.
            </span>
          </label>
        </div>
        {courses && !courses.ok && subject && (
          <div className="mt-4 rounded-lg border p-3 text-sm"
            style={{ borderColor: COLORS.amber.ink, background: COLORS.amber.soft, color: COLORS.amber.ink }}>
            {courses.detail}
          </div>
        )}
        {courses?.ok && (
          <p className="mt-3 text-sm" style={{ color: COLORS.green.ink }}>
            Connected — {courses.courses.length} course{courses.courses.length === 1 ? '' : 's'} found.
          </p>
        )}
        <button onClick={saveTop} className="mt-4 px-4 py-2 rounded-lg text-sm font-semibold text-white"
          style={{ background: ACCENT }}>
          {saved ? 'Saved' : 'Save'}
        </button>
      </section>

      {data?.classes.map((c) => (
        <ClassCard key={c.code} settings={c} courses={courses?.courses || []} viewKeys={viewKeys}
          viewTypes={data.view_types} />
      ))}

      {adding ? (
        <ClassCard settings={adding} courses={courses?.courses || []} viewKeys={viewKeys}
          viewTypes={data?.view_types || {}} isNew onDone={() => setAdding(null)} />
      ) : (
        <button onClick={() => setAdding(blank())}
          className="px-4 py-2 rounded-lg border border-gray-300 text-sm font-semibold">
          + Add a class
        </button>
      )}

      <div className="mt-6">
        <button onClick={onDone} className="text-sm font-medium underline underline-offset-4" style={{ color: ACCENT }}>
          Back to recordings
        </button>
      </div>
    </div>
  )
}

function ClassCard({
  settings,
  courses,
  viewKeys,
  viewTypes,
  isNew,
  onDone,
}: {
  settings: ClassSettings
  courses: { id: string; name: string; section: string }[]
  viewKeys: string[]
  viewTypes: Record<string, { name: string; description: string; zoom_type: string; folder: string }>
  isNew?: boolean
  onDone?: () => void
}) {
  const queryClient = useQueryClient()
  const [s, setS] = useState<ClassSettings>(settings)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const c = color(s.color)

  const set = (patch: Partial<ClassSettings>) => setS({ ...s, ...patch })

  const save = async () => {
    setError(null)
    if (!/^\d{3}$/.test(s.code)) {
      setError('Session number must be three digits, e.g. 127.')
      return
    }
    try {
      await publishApi.saveClass(s)
      setSaved(true)
      queryClient.invalidateQueries({ queryKey: ['publish-settings'] })
      queryClient.invalidateQueries({ queryKey: ['publish-queue'] })
      setTimeout(() => setSaved(false), 2500)
      onDone?.()
    } catch (e: any) {
      setError(e?.response?.data?.detail || e.message)
    }
  }

  return (
    <section className="bg-white rounded-xl border border-gray-200 p-5 mb-4"
      style={{ borderLeft: `5px solid ${c.border}` }}>
      <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
        <h3 className="text-base font-semibold">{s.label || `Session ${s.code || '___'}`}</h3>
        {!isNew && (
          <button
            onClick={async () => {
              await publishApi.deleteClass(s.code)
              queryClient.invalidateQueries({ queryKey: ['publish-settings'] })
              queryClient.invalidateQueries({ queryKey: ['publish-queue'] })
            }}
            className="text-sm text-red-600 underline underline-offset-4">
            Remove
          </button>
        )}
      </div>

      <div className="grid sm:grid-cols-2 gap-4 mb-4">
        <label className="block">
          <span className="block text-sm font-semibold mb-1">Session number</span>
          <input className="input" value={s.code} placeholder="127"
            onChange={(e) => set({ code: e.target.value.trim() })} />
        </label>
        <label className="block">
          <span className="block text-sm font-semibold mb-1">Name</span>
          <input className="input" value={s.label} placeholder="Session 127 — Mon/Wed/Fri Night"
            onChange={(e) => set({ label: e.target.value })} />
        </label>
      </div>

      <div className="grid sm:grid-cols-3 gap-4 mb-4">
        <label className="block">
          <span className="block text-sm font-semibold mb-1">Class starts</span>
          <input type="time" className="input" value={s.scheduled_start}
            onChange={(e) => set({ scheduled_start: e.target.value })} />
        </label>
        <label className="block">
          <span className="block text-sm font-semibold mb-1">Class ends</span>
          <input type="time" className="input" value={s.scheduled_end}
            onChange={(e) => set({ scheduled_end: e.target.value })} />
        </label>
        <label className="block">
          <span className="block text-sm font-semibold mb-1">Time zone</span>
          <select className="input" value={s.timezone}
            onChange={(e) => set({ timezone: e.target.value })}>
            {TIMEZONES.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
          </select>
          <span className="block text-xs text-gray-500 mt-1">
            The times above are in this zone.
          </span>
        </label>
      </div>

      <div className="mb-4">
        <span className="block text-sm font-semibold mb-1">Meets on</span>
        <div className="flex gap-1.5 flex-wrap">
          {WEEKDAYS.map((d, i) => {
            const on = s.meeting_weekdays.includes(i)
            return (
              <button key={d} type="button"
                onClick={() =>
                  set({
                    meeting_weekdays: on
                      ? s.meeting_weekdays.filter((x) => x !== i)
                      : [...s.meeting_weekdays, i].sort(),
                  })
                }
                className="px-3 py-1.5 rounded-lg border text-sm font-medium"
                style={{
                  borderColor: on ? c.ink : '#D1D5DB',
                  background: on ? c.soft : '#fff',
                  color: on ? c.ink : '#374151',
                }}>
                {d}
              </button>
            )
          })}
        </div>
      </div>

      <div className="grid sm:grid-cols-3 gap-4 mb-4">
        <label className="block">
          <span className="block text-sm font-semibold mb-1">First class date</span>
          <input type="date" className="input" value={s.first_class_date}
            onChange={(e) => set({ first_class_date: e.target.value })} />
          <span className="block text-xs text-gray-500 mt-1">Used to work out day numbers.</span>
        </label>
        <label className="block">
          <span className="block text-sm font-semibold mb-1">Keep before start</span>
          <select className="input" value={s.pad_before_minutes}
            onChange={(e) => set({ pad_before_minutes: parseInt(e.target.value, 10) })}>
            {[0, 1, 2, 5, 10].map((n) => <option key={n} value={n}>{n} min</option>)}
          </select>
        </label>
        <label className="block">
          <span className="block text-sm font-semibold mb-1">Keep after end</span>
          <select className="input" value={s.pad_after_minutes}
            onChange={(e) => set({ pad_after_minutes: parseInt(e.target.value, 10) })}>
            {[0, 2, 5, 10, 15].map((n) => <option key={n} value={n}>{n} min</option>)}
          </select>
        </label>
      </div>

      <div className="mb-4">
        <span className="block text-sm font-semibold mb-1">Videos to send every time</span>
        {viewKeys.map((k) => {
          const on = s.views.includes(k)
          return (
            <label key={k} className="flex items-center gap-3 p-2.5 rounded-lg border mb-1.5 cursor-pointer"
              style={{ borderColor: on ? c.ink : '#E5E7EB', background: on ? c.soft : '#fff' }}>
              <input type="checkbox" checked={on} className="w-4 h-4"
                onChange={(e) =>
                  set({ views: e.target.checked ? [...s.views, k] : s.views.filter((v) => v !== k) })
                } />
              <span className="text-sm">
                <b className="block font-medium">{viewTypes[k]?.name || k}</b>
                <span className="block text-xs text-gray-500">{viewTypes[k]?.description}</span>
              </span>
            </label>
          )
        })}
      </div>

      <div className="grid sm:grid-cols-2 gap-4 mb-4">
        <label className="block">
          <span className="block text-sm font-semibold mb-1">Classroom course</span>
          <select className="input" value={s.classroom_course_id}
            onChange={(e) => {
              const course = courses.find((x) => x.id === e.target.value)
              set({
                classroom_course_id: e.target.value,
                classroom_course_name: course?.name || '',
              })
            }}>
            <option value="">Not connected — Drive only</option>
            {courses.map((course) => (
              <option key={course.id} value={course.id}>
                {course.name}{course.section ? ` (${course.section})` : ''}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="block text-sm font-semibold mb-1">Filename pattern</span>
          <input className="input font-mono text-xs" value={s.filename_pattern}
            onChange={(e) => set({ filename_pattern: e.target.value })} />
          <span className="block text-xs text-gray-500 mt-1">
            {'{session} {day} {date} {view} {course}'}
          </span>
        </label>
      </div>

      {error && <p className="text-sm text-red-700 mb-3">{error}</p>}

      <div className="flex gap-2">
        <button onClick={save} className="px-4 py-2 rounded-lg text-sm font-semibold text-white"
          style={{ background: ACCENT }}>
          {saved ? 'Saved' : isNew ? 'Add class' : 'Save'}
        </button>
        {isNew && (
          <button onClick={onDone} className="px-4 py-2 rounded-lg border border-gray-300 text-sm font-semibold">
            Cancel
          </button>
        )}
      </div>
    </section>
  )
}
