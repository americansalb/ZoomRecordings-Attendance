import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  tutorApi,
  type TutorSettings,
  type TutorReminder,
  type TutorPolicy,
  type TutorSession,
  type TutorApproval,
  type TutorMessage,
} from '../../services/api'

type Tab = 'approvals' | 'sessions' | 'reminders' | 'policies' | 'messages' | 'settings'

function timeAgo(ts: number): string {
  const secs = Math.floor(Date.now() / 1000 - ts)
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return `${Math.floor(secs / 86400)}d ago`
}

export default function LiveTutorPage() {
  const [tab, setTab] = useState<Tab>('approvals')

  const { data: status } = useQuery({
    queryKey: ['tutor-status'],
    queryFn: tutorApi.getStatus,
    refetchInterval: 15000,
  })

  const pending = status?.pending_approvals ?? 0
  const tabs: { id: Tab; label: string; badge?: number }[] = [
    { id: 'approvals', label: 'Approvals', badge: pending || undefined },
    { id: 'sessions', label: 'Sessions', badge: status?.active_sessions || undefined },
    { id: 'reminders', label: 'Reminders' },
    { id: 'policies', label: 'Policies' },
    { id: 'messages', label: 'Message Log' },
    { id: 'settings', label: 'Settings' },
  ]

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Live Tutor</h1>
        <p className="mt-1 text-gray-600">
          A policy-aware in-meeting assistant. Everything the AI writes is reviewed by you before a student sees it.
        </p>
      </div>

      {/* Status banner */}
      <div className="flex flex-wrap gap-3">
        <StatusPill ok={!!status?.bot_configured}
          okText="Bot connected"
          badText="No bot configured — sends are simulated" />
        <StatusPill ok={!!status?.responder_available}
          okText="AI drafting enabled"
          badText="AI drafting off — set ANTHROPIC_API_KEY" />
        {status?.settings?.guardrails?.quiet_mode && (
          <span className="px-3 py-1 rounded-full text-sm bg-yellow-100 text-yellow-800">Quiet mode on</span>
        )}
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-200">
        <nav className="-mb-px flex flex-wrap gap-4">
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`whitespace-nowrap py-3 px-1 border-b-2 text-sm font-medium ${
                tab === t.id
                  ? 'border-indigo-500 text-indigo-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}
            >
              {t.label}
              {t.badge ? (
                <span className="ml-2 px-2 py-0.5 rounded-full bg-indigo-100 text-indigo-700 text-xs">
                  {t.badge}
                </span>
              ) : null}
            </button>
          ))}
        </nav>
      </div>

      {tab === 'approvals' && <ApprovalsTab />}
      {tab === 'sessions' && <SessionsTab />}
      {tab === 'reminders' && <RemindersTab />}
      {tab === 'policies' && <PoliciesTab />}
      {tab === 'messages' && <MessagesTab />}
      {tab === 'settings' && <SettingsTab />}
    </div>
  )
}

function StatusPill({ ok, okText, badText }: { ok: boolean; okText: string; badText: string }) {
  return (
    <span className={`px-3 py-1 rounded-full text-sm ${ok ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'}`}>
      {ok ? `✓ ${okText}` : `✕ ${badText}`}
    </span>
  )
}

// ---------------------------------------------------------------- Approvals

function ApprovalsTab() {
  const qc = useQueryClient()
  const [showHistory, setShowHistory] = useState(false)
  const [edits, setEdits] = useState<Record<number, string>>({})

  const { data: pending = [], isLoading } = useQuery({
    queryKey: ['tutor-approvals', 'pending'],
    queryFn: () => tutorApi.listApprovals('pending'),
    refetchInterval: 10000,
  })
  const { data: history = [] } = useQuery({
    queryKey: ['tutor-approvals', 'all'],
    queryFn: () => tutorApi.listApprovals(),
    enabled: showHistory,
  })

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['tutor-approvals'] })
    qc.invalidateQueries({ queryKey: ['tutor-status'] })
    qc.invalidateQueries({ queryKey: ['tutor-messages'] })
  }

  const approve = useMutation({
    mutationFn: ({ id, text }: { id: number; text?: string }) => tutorApi.approve(id, text),
    onSuccess: invalidate,
    onError: (e: any) => alert(e?.response?.data?.detail || 'Approve failed'),
  })
  const reject = useMutation({
    mutationFn: (id: number) => tutorApi.reject(id),
    onSuccess: invalidate,
    onError: (e: any) => alert(e?.response?.data?.detail || 'Reject failed'),
  })

  return (
    <div className="space-y-4">
      {isLoading ? (
        <div className="text-gray-500">Loading…</div>
      ) : pending.length === 0 ? (
        <div className="card text-center text-gray-500 py-10">
          Nothing waiting for review. AI drafts will appear here before anything is sent.
        </div>
      ) : (
        pending.map((a) => (
          <ApprovalCard
            key={a.id}
            approval={a}
            value={edits[a.id] ?? a.draft_text}
            onChange={(v) => setEdits((m) => ({ ...m, [a.id]: v }))}
            onApprove={(text) => approve.mutate({ id: a.id, text })}
            onReject={() => reject.mutate(a.id)}
            busy={approve.isPending || reject.isPending}
          />
        ))
      )}

      <button onClick={() => setShowHistory((s) => !s)} className="text-sm text-indigo-600 hover:underline">
        {showHistory ? 'Hide history' : 'Show decided history'}
      </button>
      {showHistory && (
        <div className="card">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left text-gray-500">
                <th className="py-2 pr-4">When</th>
                <th className="py-2 pr-4">Channel</th>
                <th className="py-2 pr-4">Status</th>
                <th className="py-2 pr-4">Text</th>
                <th className="py-2 pr-4">By</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {history.filter((h) => h.status !== 'pending').map((h) => (
                <tr key={h.id}>
                  <td className="py-2 pr-4 whitespace-nowrap text-gray-500">{timeAgo(h.created_at)}</td>
                  <td className="py-2 pr-4">{channelLabel(h)}</td>
                  <td className="py-2 pr-4">{statusBadge(h.status)}</td>
                  <td className="py-2 pr-4 max-w-md truncate">{h.final_text || h.draft_text}</td>
                  <td className="py-2 pr-4 text-gray-500">{h.decided_by || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function ApprovalCard({
  approval, value, onChange, onApprove, onReject, busy,
}: {
  approval: TutorApproval
  value: string
  onChange: (v: string) => void
  onApprove: (text?: string) => void
  onReject: () => void
  busy: boolean
}) {
  const ctx = approval.context || {}
  const question = (ctx as any).question as string | undefined
  const instruction = (ctx as any).instruction as string | undefined
  const rationale = (ctx as any).rationale as string | undefined
  const edited = value !== approval.draft_text

  return (
    <div className="card border-l-4 border-indigo-400">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2 text-sm">
          <span className="px-2 py-0.5 rounded bg-indigo-100 text-indigo-700">{sourceLabel(approval.source)}</span>
          <span className="px-2 py-0.5 rounded bg-gray-100 text-gray-700">{channelLabel(approval)}</span>
          {approval.confidence && (
            <span className="px-2 py-0.5 rounded bg-gray-100 text-gray-600">confidence: {approval.confidence}</span>
          )}
        </div>
        <span className="text-xs text-gray-400">{timeAgo(approval.created_at)}</span>
      </div>

      {(question || instruction) && (
        <p className="text-sm text-gray-600 mb-1">
          <span className="font-medium">{question ? 'Student asked:' : 'Instruction:'}</span>{' '}
          {question || instruction}
        </p>
      )}
      {rationale && <p className="text-xs text-gray-400 mb-2">AI note: {rationale}</p>}

      <textarea
        className="w-full border border-gray-300 rounded p-2 text-sm"
        rows={3}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <div className="flex items-center gap-2 mt-2">
        <button
          className="btn btn-primary text-sm disabled:opacity-50"
          disabled={busy || !value.trim()}
          onClick={() => onApprove(edited ? value : undefined)}
        >
          {edited ? 'Approve edited & send' : 'Approve & send'}
        </button>
        <button
          className="text-sm px-3 py-1.5 rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          disabled={busy}
          onClick={onReject}
        >
          Reject
        </button>
      </div>
    </div>
  )
}

// ----------------------------------------------------------------- Sessions

function SessionsTab() {
  const qc = useQueryClient()
  const { data: sessions = [], isLoading } = useQuery({
    queryKey: ['tutor-sessions'],
    queryFn: tutorApi.listSessions,
    refetchInterval: 10000,
  })
  const { data: reminders = [] } = useQuery({ queryKey: ['tutor-reminders'], queryFn: tutorApi.listReminders })

  const [form, setForm] = useState({ meeting_id: '', topic: '', session_code: '' })
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['tutor-sessions'] })
    qc.invalidateQueries({ queryKey: ['tutor-status'] })
  }
  const summon = useMutation({
    mutationFn: () => tutorApi.summon({
      meeting_id: form.meeting_id.trim(),
      topic: form.topic.trim() || undefined,
      session_code: form.session_code.trim() || undefined,
    }),
    onSuccess: () => { setForm({ meeting_id: '', topic: '', session_code: '' }); invalidate() },
    onError: (e: any) => alert(e?.response?.data?.detail || 'Summon failed'),
  })

  return (
    <div className="space-y-6">
      <div className="card">
        <h3 className="font-semibold text-gray-900 mb-3">Summon the bot into a meeting</h3>
        <div className="grid grid-cols-1 sm:grid-cols-4 gap-2">
          <input className="border border-gray-300 rounded p-2 text-sm" placeholder="Zoom meeting ID *"
            value={form.meeting_id} onChange={(e) => setForm({ ...form, meeting_id: e.target.value })} />
          <input className="border border-gray-300 rounded p-2 text-sm" placeholder="Topic (optional)"
            value={form.topic} onChange={(e) => setForm({ ...form, topic: e.target.value })} />
          <input className="border border-gray-300 rounded p-2 text-sm" placeholder="Session code (optional)"
            value={form.session_code} onChange={(e) => setForm({ ...form, session_code: e.target.value })} />
          <button className="btn btn-primary text-sm disabled:opacity-50"
            disabled={!form.meeting_id.trim() || summon.isPending}
            onClick={() => summon.mutate()}>
            {summon.isPending ? 'Summoning…' : 'Summon'}
          </button>
        </div>
      </div>

      {isLoading ? (
        <div className="text-gray-500">Loading…</div>
      ) : sessions.length === 0 ? (
        <div className="card text-center text-gray-500 py-10">The bot isn’t in any meetings right now.</div>
      ) : (
        sessions.map((s) => (
          <SessionCard key={s.id} session={s} reminders={reminders} onChanged={invalidate} />
        ))
      )}
    </div>
  )
}

function SessionCard({ session, reminders, onChanged }: {
  session: TutorSession
  reminders: TutorReminder[]
  onChanged: () => void
}) {
  const qc = useQueryClient()
  const [reminderId, setReminderId] = useState<number | ''>('')
  const [publicText, setPublicText] = useState('')
  const [dm, setDm] = useState({ target_id: '', target_name: '', text: '' })
  const [aiDm, setAiDm] = useState({ target_id: '', target_name: '', instruction: '' })
  const [sim, setSim] = useState('')

  const afterSend = () => {
    qc.invalidateQueries({ queryKey: ['tutor-messages'] })
    qc.invalidateQueries({ queryKey: ['tutor-approvals'] })
    qc.invalidateQueries({ queryKey: ['tutor-status'] })
  }
  const fail = (e: any) => alert(e?.response?.data?.detail || 'Action failed')

  const dismiss = useMutation({ mutationFn: () => tutorApi.dismiss(session.id), onSuccess: onChanged, onError: fail })
  const postReminder = useMutation({
    mutationFn: () => tutorApi.postReminder(session.id, { reminder_id: Number(reminderId) }),
    onSuccess: () => { setReminderId(''); afterSend() }, onError: fail,
  })
  const sendPublic = useMutation({
    mutationFn: () => tutorApi.sendMessage(session.id, { channel: 'public', text: publicText.trim() }),
    onSuccess: () => { setPublicText(''); afterSend() }, onError: fail,
  })
  const sendDm = useMutation({
    mutationFn: () => tutorApi.sendMessage(session.id, {
      channel: 'dm', text: dm.text.trim(), target_id: dm.target_id.trim(), target_name: dm.target_name.trim() || undefined,
    }),
    onSuccess: () => { setDm({ target_id: '', target_name: '', text: '' }); afterSend() }, onError: fail,
  })
  const requestAiDm = useMutation({
    mutationFn: () => tutorApi.requestAiDm(session.id, {
      target_id: aiDm.target_id.trim(), target_name: aiDm.target_name.trim() || undefined, instruction: aiDm.instruction.trim(),
    }),
    onSuccess: () => { setAiDm({ target_id: '', target_name: '', instruction: '' }); afterSend() }, onError: fail,
  })
  const simulate = useMutation({
    mutationFn: () => tutorApi.simulateInbound(session.id, { text: sim.trim(), participant_name: 'Test student' }),
    onSuccess: (r) => { setSim(''); afterSend(); if (!r.drafted) alert('No draft created (AI off, abstained, or guardrail).') },
    onError: fail,
  })

  return (
    <div className="card">
      <div className="flex items-start justify-between">
        <div>
          <div className="font-semibold text-gray-900">{session.topic || `Meeting ${session.meeting_id}`}</div>
          <div className="text-sm text-gray-500">
            ID {session.meeting_id}{session.session_code ? ` · Session ${session.session_code}` : ''} · {statusBadge(session.status)}
            {session.summoned_by ? ` · by ${session.summoned_by}` : ''}
          </div>
        </div>
        <button className="text-sm px-3 py-1.5 rounded border border-red-300 text-red-700 hover:bg-red-50"
          onClick={() => dismiss.mutate()} disabled={dismiss.isPending}>
          {dismiss.isPending ? 'Dismissing…' : 'Dismiss'}
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4">
        {/* Post a reminder */}
        <div>
          <label className="text-xs font-medium text-gray-500">Post a reminder (public)</label>
          <div className="flex gap-2 mt-1">
            <select className="border border-gray-300 rounded p-2 text-sm flex-1"
              value={reminderId} onChange={(e) => setReminderId(e.target.value ? Number(e.target.value) : '')}>
              <option value="">Choose a reminder…</option>
              {reminders.filter((r) => r.enabled).map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
            </select>
            <button className="btn btn-primary text-sm disabled:opacity-50"
              disabled={!reminderId || postReminder.isPending} onClick={() => postReminder.mutate()}>Post</button>
          </div>
        </div>

        {/* Manual public message */}
        <div>
          <label className="text-xs font-medium text-gray-500">Type a public message</label>
          <div className="flex gap-2 mt-1">
            <input className="border border-gray-300 rounded p-2 text-sm flex-1" placeholder="Message to chat"
              value={publicText} onChange={(e) => setPublicText(e.target.value)} />
            <button className="btn btn-primary text-sm disabled:opacity-50"
              disabled={!publicText.trim() || sendPublic.isPending} onClick={() => sendPublic.mutate()}>Send</button>
          </div>
        </div>

        {/* Manual DM */}
        <div>
          <label className="text-xs font-medium text-gray-500">Direct message (you type it)</label>
          <div className="flex flex-col gap-2 mt-1">
            <div className="flex gap-2">
              <input className="border border-gray-300 rounded p-2 text-sm w-1/2" placeholder="Participant ID"
                value={dm.target_id} onChange={(e) => setDm({ ...dm, target_id: e.target.value })} />
              <input className="border border-gray-300 rounded p-2 text-sm w-1/2" placeholder="Name (optional)"
                value={dm.target_name} onChange={(e) => setDm({ ...dm, target_name: e.target.value })} />
            </div>
            <div className="flex gap-2">
              <input className="border border-gray-300 rounded p-2 text-sm flex-1" placeholder="DM text"
                value={dm.text} onChange={(e) => setDm({ ...dm, text: e.target.value })} />
              <button className="btn btn-primary text-sm disabled:opacity-50"
                disabled={!dm.target_id.trim() || !dm.text.trim() || sendDm.isPending} onClick={() => sendDm.mutate()}>Send</button>
            </div>
          </div>
        </div>

        {/* AI-drafted DM (goes to approvals) */}
        <div>
          <label className="text-xs font-medium text-gray-500">Ask AI to draft a DM → review queue</label>
          <div className="flex flex-col gap-2 mt-1">
            <div className="flex gap-2">
              <input className="border border-gray-300 rounded p-2 text-sm w-1/2" placeholder="Participant ID"
                value={aiDm.target_id} onChange={(e) => setAiDm({ ...aiDm, target_id: e.target.value })} />
              <input className="border border-gray-300 rounded p-2 text-sm w-1/2" placeholder="Name (optional)"
                value={aiDm.target_name} onChange={(e) => setAiDm({ ...aiDm, target_name: e.target.value })} />
            </div>
            <div className="flex gap-2">
              <input className="border border-gray-300 rounded p-2 text-sm flex-1" placeholder="What should it say?"
                value={aiDm.instruction} onChange={(e) => setAiDm({ ...aiDm, instruction: e.target.value })} />
              <button className="text-sm px-3 py-1.5 rounded border border-indigo-300 text-indigo-700 hover:bg-indigo-50 disabled:opacity-50"
                disabled={!aiDm.target_id.trim() || !aiDm.instruction.trim() || requestAiDm.isPending}
                onClick={() => requestAiDm.mutate()}>Draft</button>
            </div>
          </div>
        </div>
      </div>

      {/* Test helper */}
      <div className="mt-4 pt-3 border-t border-gray-100">
        <label className="text-xs font-medium text-gray-500">Test: simulate a student question (no message is sent to the meeting)</label>
        <div className="flex gap-2 mt-1">
          <input className="border border-gray-300 rounded p-2 text-sm flex-1" placeholder="e.g. When does class start?"
            value={sim} onChange={(e) => setSim(e.target.value)} />
          <button className="text-sm px-3 py-1.5 rounded border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            disabled={!sim.trim() || simulate.isPending} onClick={() => simulate.mutate()}>Simulate</button>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- Reminders

function RemindersTab() {
  const qc = useQueryClient()
  const { data: reminders = [] } = useQuery({ queryKey: ['tutor-reminders'], queryFn: tutorApi.listReminders })
  const [form, setForm] = useState({ label: '', message: '' })
  const invalidate = () => qc.invalidateQueries({ queryKey: ['tutor-reminders'] })

  const create = useMutation({
    mutationFn: () => tutorApi.createReminder({ label: form.label.trim(), message: form.message.trim() }),
    onSuccess: () => { setForm({ label: '', message: '' }); invalidate() },
  })
  const toggle = useMutation({
    mutationFn: (r: TutorReminder) => tutorApi.updateReminder(r.id, { enabled: !r.enabled }),
    onSuccess: invalidate,
  })
  const remove = useMutation({ mutationFn: (id: number) => tutorApi.deleteReminder(id), onSuccess: invalidate })

  return (
    <div className="space-y-4">
      <div className="card">
        <h3 className="font-semibold text-gray-900 mb-3">New reminder template</h3>
        <div className="space-y-2">
          <input className="border border-gray-300 rounded p-2 text-sm w-full" placeholder="Label (e.g. Cameras on)"
            value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} />
          <textarea className="border border-gray-300 rounded p-2 text-sm w-full" rows={2} placeholder="Message posted to chat"
            value={form.message} onChange={(e) => setForm({ ...form, message: e.target.value })} />
          <button className="btn btn-primary text-sm disabled:opacity-50"
            disabled={!form.label.trim() || !form.message.trim() || create.isPending} onClick={() => create.mutate()}>
            Add reminder
          </button>
        </div>
      </div>

      {reminders.length === 0 ? (
        <div className="card text-center text-gray-500 py-8">No reminders yet.</div>
      ) : (
        <div className="card divide-y divide-gray-100">
          {reminders.map((r) => (
            <div key={r.id} className="py-3 flex items-start justify-between gap-4">
              <div>
                <div className="font-medium text-gray-900">{r.label}</div>
                <div className="text-sm text-gray-600">{r.message}</div>
              </div>
              <div className="flex items-center gap-3 shrink-0">
                <label className="text-sm text-gray-500 flex items-center gap-1">
                  <input type="checkbox" checked={!!r.enabled} onChange={() => toggle.mutate(r)} /> enabled
                </label>
                <button className="text-sm text-red-600 hover:underline" onClick={() => remove.mutate(r.id)}>Delete</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ----------------------------------------------------------------- Policies

function PoliciesTab() {
  const qc = useQueryClient()
  const { data: policies = [] } = useQuery({ queryKey: ['tutor-policies'], queryFn: tutorApi.listPolicies })
  const [form, setForm] = useState({ title: '', content: '' })
  const invalidate = () => qc.invalidateQueries({ queryKey: ['tutor-policies'] })

  const create = useMutation({
    mutationFn: () => tutorApi.createPolicy({ title: form.title.trim(), content: form.content.trim() }),
    onSuccess: () => { setForm({ title: '', content: '' }); invalidate() },
  })
  const toggle = useMutation({
    mutationFn: (p: TutorPolicy) => tutorApi.updatePolicy(p.id, { enabled: !p.enabled }),
    onSuccess: invalidate,
  })
  const remove = useMutation({ mutationFn: (id: number) => tutorApi.deletePolicy(id), onSuccess: invalidate })

  return (
    <div className="space-y-4">
      <div className="card">
        <h3 className="font-semibold text-gray-900 mb-1">New policy</h3>
        <p className="text-sm text-gray-500 mb-3">
          Policies steer how the AI answers. Enabled policies are given to Opus 4.8 as the rules it must stay within.
        </p>
        <div className="space-y-2">
          <input className="border border-gray-300 rounded p-2 text-sm w-full" placeholder="Title (e.g. Start time)"
            value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
          <textarea className="border border-gray-300 rounded p-2 text-sm w-full" rows={3} placeholder="The rule / answer guidance"
            value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })} />
          <button className="btn btn-primary text-sm disabled:opacity-50"
            disabled={!form.title.trim() || !form.content.trim() || create.isPending} onClick={() => create.mutate()}>
            Add policy
          </button>
        </div>
      </div>

      {policies.length === 0 ? (
        <div className="card text-center text-gray-500 py-8">No policies yet.</div>
      ) : (
        <div className="card divide-y divide-gray-100">
          {policies.map((p) => (
            <div key={p.id} className="py-3 flex items-start justify-between gap-4">
              <div>
                <div className="font-medium text-gray-900">{p.title}</div>
                <div className="text-sm text-gray-600">{p.content}</div>
              </div>
              <div className="flex items-center gap-3 shrink-0">
                <label className="text-sm text-gray-500 flex items-center gap-1">
                  <input type="checkbox" checked={!!p.enabled} onChange={() => toggle.mutate(p)} /> enabled
                </label>
                <button className="text-sm text-red-600 hover:underline" onClick={() => remove.mutate(p.id)}>Delete</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ----------------------------------------------------------------- Messages

function MessagesTab() {
  const { data: messages = [], isLoading } = useQuery({
    queryKey: ['tutor-messages'],
    queryFn: () => tutorApi.listMessages({ limit: 300 }),
    refetchInterval: 15000,
  })

  return (
    <div className="card">
      <h3 className="font-semibold text-gray-900 mb-3">Message log (every inbound & outbound message)</h3>
      {isLoading ? (
        <div className="text-gray-500">Loading…</div>
      ) : messages.length === 0 ? (
        <div className="text-center text-gray-500 py-8">No messages yet.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full text-sm">
            <thead>
              <tr className="text-left text-gray-500">
                <th className="py-2 pr-4">When</th>
                <th className="py-2 pr-4">Dir</th>
                <th className="py-2 pr-4">Channel</th>
                <th className="py-2 pr-4">Who</th>
                <th className="py-2 pr-4">Text</th>
                <th className="py-2 pr-4">Source</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {messages.map((m: TutorMessage) => (
                <tr key={m.id}>
                  <td className="py-2 pr-4 whitespace-nowrap text-gray-500">{timeAgo(m.created_at)}</td>
                  <td className="py-2 pr-4">
                    <span className={m.direction === 'inbound' ? 'text-blue-600' : 'text-green-700'}>
                      {m.direction === 'inbound' ? '↓ in' : '↑ out'}
                    </span>
                  </td>
                  <td className="py-2 pr-4">{m.channel === 'dm' ? 'DM' : 'public'}</td>
                  <td className="py-2 pr-4 whitespace-nowrap">{m.participant_name || '—'}</td>
                  <td className="py-2 pr-4 max-w-md">{m.text}</td>
                  <td className="py-2 pr-4 text-gray-500">{m.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ----------------------------------------------------------------- Settings

function SettingsTab() {
  const qc = useQueryClient()
  const { data: settings, isLoading } = useQuery({ queryKey: ['tutor-settings'], queryFn: tutorApi.getSettings })
  const [draft, setDraft] = useState<TutorSettings | null>(null)
  const current = draft ?? settings ?? null

  const save = useMutation({
    mutationFn: (patch: Partial<TutorSettings>) => tutorApi.patchSettings(patch),
    onSuccess: (s) => {
      setDraft(null)
      qc.setQueryData(['tutor-settings'], s)
      qc.invalidateQueries({ queryKey: ['tutor-status'] })
    },
  })

  if (isLoading || !current) return <div className="text-gray-500">Loading…</div>

  const set = (patch: Partial<TutorSettings>) =>
    setDraft({ ...current, ...patch, capabilities: { ...current.capabilities, ...(patch.capabilities || {}) },
      guardrails: { ...current.guardrails, ...(patch.guardrails || {}) },
      bot: { ...current.bot, ...(patch.bot || {}) } })

  const caps: { key: keyof TutorSettings['capabilities']; label: string; note?: string }[] = [
    { key: 'reminders', label: 'Reminders' },
    { key: 'answer_questions', label: 'Answer questions', note: 'Experimental — may be distracting. Drafts still require approval.' },
    { key: 'direct_messages', label: 'Direct messages' },
    { key: 'summon_dismiss', label: 'Summon / dismiss' },
  ]

  return (
    <div className="space-y-6 max-w-3xl">
      <div className="card">
        <h3 className="font-semibold text-gray-900 mb-3">Capabilities</h3>
        <div className="space-y-3">
          {caps.map((c) => (
            <label key={c.key} className="flex items-start gap-3">
              <input type="checkbox" className="mt-1" checked={!!current.capabilities[c.key]}
                onChange={(e) => set({ capabilities: { [c.key]: e.target.checked } as any })} />
              <span>
                <span className="font-medium text-gray-900">{c.label}</span>
                {c.note && <span className="block text-xs text-amber-600">{c.note}</span>}
              </span>
            </label>
          ))}
        </div>
      </div>

      <div className="card">
        <h3 className="font-semibold text-gray-900 mb-3">Anti-distraction guardrails</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <label className="text-sm">
            <span className="block text-gray-600 mb-1">Min seconds between messages</span>
            <input type="number" min={0} className="border border-gray-300 rounded p-2 w-full"
              value={current.guardrails.min_seconds_between_messages}
              onChange={(e) => set({ guardrails: { min_seconds_between_messages: Number(e.target.value) } as any })} />
          </label>
          <label className="text-sm">
            <span className="block text-gray-600 mb-1">Max AI messages per session</span>
            <input type="number" min={0} className="border border-gray-300 rounded p-2 w-full"
              value={current.guardrails.max_ai_messages_per_session}
              onChange={(e) => set({ guardrails: { max_ai_messages_per_session: Number(e.target.value) } as any })} />
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={current.guardrails.quiet_mode}
              onChange={(e) => set({ guardrails: { quiet_mode: e.target.checked } as any })} />
            <span className="text-gray-700">Quiet mode (pause all outbound AI activity)</span>
          </label>
        </div>
      </div>

      <div className="card">
        <h3 className="font-semibold text-gray-900 mb-3">Bot identity & announcement</h3>
        <div className="space-y-3">
          <label className="text-sm block">
            <span className="block text-gray-600 mb-1">Display name in the meeting</span>
            <input className="border border-gray-300 rounded p-2 w-full"
              value={current.bot.display_name}
              onChange={(e) => set({ bot: { display_name: e.target.value } as any })} />
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={current.bot.announce_on_join}
              onChange={(e) => set({ bot: { announce_on_join: e.target.checked } as any })} />
            <span className="text-gray-700">Announce the bot when it joins (recommended for transparency)</span>
          </label>
          <label className="text-sm block">
            <span className="block text-gray-600 mb-1">Announcement text</span>
            <textarea rows={2} className="border border-gray-300 rounded p-2 w-full"
              value={current.bot.announcement}
              onChange={(e) => set({ bot: { announcement: e.target.value } as any })} />
          </label>
        </div>
      </div>

      <div className="flex items-center gap-3">
        <button className="btn btn-primary disabled:opacity-50" disabled={!draft || save.isPending}
          onClick={() => draft && save.mutate(draft)}>
          {save.isPending ? 'Saving…' : 'Save settings'}
        </button>
        {draft && <button className="text-sm text-gray-500 hover:underline" onClick={() => setDraft(null)}>Discard changes</button>}
        {save.isSuccess && !draft && <span className="text-sm text-green-600">Saved.</span>}
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ helpers

function sourceLabel(source: string): string {
  return { ai_answer: 'AI answer', ai_dm: 'AI DM', reminder: 'Reminder', manual: 'Manual' }[source] || source
}

function channelLabel(a: TutorApproval): string {
  if (a.channel === 'dm') return `DM${a.target_name ? ` → ${a.target_name}` : ''}`
  return 'Public'
}

function statusBadge(status: string) {
  const map: Record<string, string> = {
    sent: 'bg-green-100 text-green-800',
    rejected: 'bg-gray-100 text-gray-600',
    failed: 'bg-red-100 text-red-800',
    pending: 'bg-yellow-100 text-yellow-800',
    in_meeting: 'bg-green-100 text-green-800',
    requested: 'bg-yellow-100 text-yellow-800',
    joining: 'bg-yellow-100 text-yellow-800',
    left: 'bg-gray-100 text-gray-600',
    error: 'bg-red-100 text-red-800',
  }
  return <span className={`px-2 py-0.5 rounded text-xs ${map[status] || 'bg-gray-100 text-gray-600'}`}>{status}</span>
}
