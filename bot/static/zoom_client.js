/*
 * INTEGRATION POINT — Zoom Web Meeting SDK (Component View) glue.
 *
 * Playwright (meeting_client.py) calls the window.zoom* functions below and
 * registers window.onZoomChat / window.onZoomLifecycle to receive events.
 *
 * The method names here are verified against the Component View client object
 * that ZoomMtgEmbedded.createClient() actually returns. That object exposes 67
 * methods and NONE of the following, despite what a lot of Zoom sample code
 * implies: getChatClient, getMediaStream, getAllUser, leave. Calling any of
 * them throws a TypeError. The correct names are sendChat, getAttendeeslist
 * and leaveMeeting. Checked against 3.13.2 and 6.2.0; both agree.
 *
 * Attendance is derived from participant identity and camera state, never from
 * tile position: getAttendeeslist() plus the user-added/user-removed/
 * user-updated events give us who was in the room and when.
 */


// Zoom's SDK rejects with a plain object ({ type, reason, errorCode }),
// not an Error. Playwright stringifies that as the literal word
// "Object", so the reason is lost exactly when it matters most. Convert
// to a real Error carrying the JSON, so the failure text that reaches
// the operator says what Zoom actually objected to.
function zoomError(prefix, e) {
  if (e instanceof Error) return new Error(`${prefix}: ${e.message}`);
  let body;
  try { body = JSON.stringify(e); } catch { body = String(e); }
  return new Error(`${prefix}: ${body}`);
}

let client = null;
let selfUserId = null;
let joined = false;

/*
 * Presence ledger — the actual attendance record.
 *
 * Keyed by Zoom user id. Zoom reuses display names and reassigns tile
 * positions freely, but a user id is stable for the length of one join, so
 * that is what everything is attributed to. A participant who drops and
 * rejoins gets a new id from Zoom; we keep both stints under the name and let
 * the backend decide whether to merge them.
 *
 *   { userId, name, joinedAt, leftAt, videoOnMs, videoOn, lastChangeAt }
 *
 * videoOnMs accumulates only on transitions, so the numbers do not depend on
 * how often Python polls us.
 */
const presence = new Map();

function nowMs() { return Date.now(); }

function ledgerFor(userId, name) {
  const id = String(userId);
  let row = presence.get(id);
  if (!row) {
    row = {
      userId: id,
      name: name || '',
      joinedAt: nowMs(),
      leftAt: null,
      videoOn: false,
      videoOnMs: 0,
      lastChangeAt: nowMs(),
    };
    presence.set(id, row);
  }
  if (name && !row.name) row.name = name;
  return row;
}

// Fold the time since the last transition into the running total, then record
// the new state. Called on every video state change and on every read, so a
// participant whose camera has been on for 20 minutes without any event still
// reports 20 minutes.
function settleVideo(row, videoOn, at) {
  const t = at || nowMs();
  if (row.videoOn && row.leftAt === null) row.videoOnMs += Math.max(0, t - row.lastChangeAt);
  row.videoOn = !!videoOn;
  row.lastChangeAt = t;
}

function participantVideoOn(u) {
  // `video` is the current field; `bVideoOn` is the pre-4.0 name. 3.13.2 sets
  // both, 6.x deprecates the latter. Read whichever is present so an SDK
  // upgrade does not silently turn every camera "off".
  if (typeof u.video === 'boolean') return u.video;
  return !!u.bVideoOn;
}

function participantName(u) {
  return u.displayName || u.userName || '';
}

function emitLifecycle(type, detail) {
  try {
    if (window.onZoomLifecycle) window.onZoomLifecycle({ type, detail: detail || null });
  } catch (e) { console.error('lifecycle handler error', e); }
}

async function ensureClient() {
  if (client) return client;
  client = ZoomMtgEmbedded.createClient();
  try {
    await client.init({
      zoomAppRoot: document.getElementById('zoom-root'),
      language: 'en-US',
      // Without this the SDK fetches its audio/video wasm and workers from
      // https://source.zoom.us/<version>/lib/av at join time. This page is
      // served under Cross-Origin-Embedder-Policy: require-corp, where a
      // cross-origin fetch has several ways to be dropped without a usable
      // error, and the whole point of vendoring the SDK into the image was to
      // stop depending on that CDN. The assets ship in the same npm tarball
      // and are mounted at /lib by app.py.
      assetPath: `${window.location.origin}/lib/av`,
      // Pulls a media patch from the CDN at runtime, which reintroduces exactly
      // the cross-origin fetch assetPath just removed, and floats the media
      // version away from the pinned SDK.
      patchJsMedia: false,
    });
  } catch (e) {
    client = null;               // let a retry re-init rather than reusing a dead client
    throw zoomError('Zoom SDK init failed', e);
  }

  // Inbound chat -> forward to the Python side (ignoring our own messages).
  client.on('chat-on-message', (payload) => {
    try {
      const sender = payload.sender || {};
      if (selfUserId && String(sender.userId) === String(selfUserId)) return;
      const receiver = payload.receiver || {};
      // Private if addressed to a specific user (i.e. a DM to the bot).
      const isPrivate = !!(receiver && receiver.userId);
      if (window.onZoomChat) {
        window.onZoomChat({
          message: payload.message,
          sender: { name: sender.name, userId: String(sender.userId) },
          isPrivate: isPrivate,
        });
      }
    } catch (e) { console.error('chat handler error', e); }
  });

  // --- attendance signal -------------------------------------------------
  // These three events are the only reliable way to know who was in the room
  // and when. Polling getAttendeeslist() alone misses anyone who joins and
  // leaves between two polls, which for a 5 minute cadence is most of a late
  // arrival's excuse.
  client.on('user-added', (payload) => {
    try {
      for (const u of [].concat(payload || [])) {
        const row = ledgerFor(u.userId, participantName(u));
        row.leftAt = null;
        settleVideo(row, participantVideoOn(u));
      }
    } catch (e) { console.error('user-added handler error', e); }
  });

  client.on('user-removed', (payload) => {
    try {
      for (const u of [].concat(payload || [])) {
        const row = presence.get(String(u.userId));
        if (!row) continue;
        settleVideo(row, false);
        row.leftAt = nowMs();
      }
    } catch (e) { console.error('user-removed handler error', e); }
  });

  client.on('user-updated', (payload) => {
    try {
      for (const u of [].concat(payload || [])) {
        const row = presence.get(String(u.userId));
        if (!row) continue;
        if (u.displayName || u.userName) row.name = participantName(u);
        // A user-updated payload is a partial: it carries only what changed,
        // so an absent video field means "unchanged", not "off".
        if ('video' in u || 'bVideoOn' in u) settleVideo(row, participantVideoOn(u));
      }
    } catch (e) { console.error('user-updated handler error', e); }
  });

  // The meeting ending is the one event that must reach Python. Without it the
  // headless browser sits in a dead meeting until something else kills it.
  client.on('connection-change', (payload) => {
    try {
      const state = payload && payload.state;
      if (state === 'Closed' || state === 'Fail') {
        joined = false;
        for (const row of presence.values()) {
          if (row.leftAt === null) { settleVideo(row, false); row.leftAt = nowMs(); }
        }
        emitLifecycle('ended', String(state));
      }
    } catch (e) { console.error('connection-change handler error', e); }
  });

  return client;
}

window.zoomJoin = async (cfg) => {
  await ensureClient();
  const joinArgs = {
    sdkKey: cfg.sdkKey,
    signature: cfg.signature,
    meetingNumber: cfg.meetingNumber,
    password: cfg.passcode || '',
    userName: cfg.userName,
  };
  // Zoom requires apps joining meetings to be authorized (March 2 2026);
  // a ZAK is one of the accepted mechanisms. It also joins us AS the
  // bot's own Zoom user rather than as an anonymous guest, which is what
  // lets an alternative-host assignment grant co-host rights. Without
  // those, a host who restricts chat to "Host only" silently disables
  // every direct message.
  //
  // Only set when present, so a join without one behaves exactly as
  // before on accounts that still permit it.
  if (cfg.zak) joinArgs.zak = cfg.zak;
  try {
    await client.join(joinArgs);
  } catch (e) {
    throw zoomError('Zoom join rejected', e);
  }
  joined = true;

  // Everything past this point is bookkeeping. It used to run unguarded, and
  // one bad call here (getMediaStream, which this SDK does not have) rejected
  // zoomJoin *after* the join had already succeeded. Python then treated the
  // whole join as failed and dropped the session on the floor, while the
  // browser stayed in the meeting: a silent bot nobody could dismiss. A
  // failure to read our own user id is not a failed join, so it is logged and
  // swallowed rather than being allowed to reach Python.
  try {
    const me = client.getCurrentUser && client.getCurrentUser();
    selfUserId = me ? String(me.userId) : null;
  } catch (e) {
    selfUserId = null;
    console.warn('could not read self user id', e);
  }

  // Seed the ledger with whoever is already here. user-added only fires for
  // people who arrive after us.
  try {
    for (const u of client.getAttendeeslist() || []) {
      const row = ledgerFor(u.userId, participantName(u));
      settleVideo(row, participantVideoOn(u));
    }
  } catch (e) { console.warn('could not seed participant list', e); }

  emitLifecycle('joined', null);
};

window.zoomSendChat = async (text, toUserId) => {
  if (!client || !joined) throw new Error('cannot send chat: not in a meeting');
  // sendChat(message, userId?) — omitting the id sends to everyone. There is
  // no getChatClient() on the Component View client and no sendToAll().
  if (toUserId !== null && toUserId !== undefined && toUserId !== '') {
    await client.sendChat(text, Number(toUserId));
  } else {
    await client.sendChat(text);
  }
};

window.zoomListUsers = async () => {
  if (!client) return [];
  const users = client.getAttendeeslist() || [];
  return users.map((u) => ({
    userId: String(u.userId),
    displayName: participantName(u),
    bVideoOn: participantVideoOn(u),
    isHost: !!u.isHost,
    isCoHost: !!u.isCoHost,
  }));
};

window.zoomSelfUserId = async () => selfUserId;

/*
 * Raw diagnostics: what the SDK itself reports, before any of our mapping.
 *
 * Camera state is the one field the whole participation rule rests on, and if
 * it disagrees with what a person can see in their own Zoom window, guessing
 * at the cause from a summary count is hopeless. This returns the untouched
 * participant objects so the disagreement can be read directly, including
 * which field the value came from and whether the media engine ever started.
 */
window.zoomDiagnostics = async () => {
  const out = {
    joined: joined,
    selfUserId: selfUserId,
    sdkVersion: (window.ZoomMtgEmbedded && ZoomMtgEmbedded.VERSION) || null,
    crossOriginIsolated: self.crossOriginIsolated === true,
    assetPath: `${window.location.origin}/lib/av`,
    raw: [],
    error: null,
  };
  if (!client) { out.error = 'no client'; return out; }
  try {
    const users = client.getAttendeeslist() || [];
    out.raw = users.map((u) => ({
      userId: String(u.userId),
      displayName: u.displayName ?? null,
      userName: u.userName ?? null,
      // Both spellings, unmapped, so a disagreement between them is visible.
      video: (typeof u.video === 'undefined') ? '(absent)' : u.video,
      bVideoOn: (typeof u.bVideoOn === 'undefined') ? '(absent)' : u.bVideoOn,
      isVideoConnect: u.isVideoConnect ?? u.bVideoConnect ?? '(absent)',
      muted: u.muted ?? '(absent)',
      isHost: !!u.isHost,
      isCoHost: !!u.isCoHost,
      // What our own mapping concludes from the above.
      resolvedVideoOn: participantVideoOn(u),
    }));
  } catch (e) {
    out.error = String(e).slice(0, 300);
  }
  try {
    const me = client.getCurrentUser && client.getCurrentUser();
    out.currentUser = me ? { userId: String(me.userId), displayName: me.displayName } : null;
  } catch (e) { out.currentUser = null; }
  return out;
};

/*
 * The attendance read. Returns one row per participant seen this meeting,
 * with camera time settled up to the moment of the call so the caller does
 * not have to know about transitions.
 */
window.zoomPresence = async () => {
  const t = nowMs();
  const rows = [];
  // Reconcile against the live roster first. Events are the primary signal but
  // they can be missed (a dropped websocket frame, a handler that threw), and
  // a roster read is cheap insurance against a participant who is present but
  // absent from our ledger.
  try {
    for (const u of client.getAttendeeslist() || []) {
      const row = ledgerFor(u.userId, participantName(u));
      if (row.leftAt !== null) row.leftAt = null;   // rejoined
      const live = participantVideoOn(u);
      if (live !== row.videoOn) settleVideo(row, live, t);
    }
  } catch (e) { console.warn('presence roster read failed', e); }

  for (const row of presence.values()) {
    // Settle without changing state, so repeated reads are not double counted.
    const open = row.videoOn && row.leftAt === null;
    const videoOnMs = row.videoOnMs + (open ? Math.max(0, t - row.lastChangeAt) : 0);
    rows.push({
      userId: row.userId,
      name: row.name,
      joinedAt: row.joinedAt / 1000,
      leftAt: row.leftAt === null ? null : row.leftAt / 1000,
      present: row.leftAt === null,
      videoOn: row.videoOn,
      videoOnSeconds: Math.round(videoOnMs / 1000),
      observedSeconds: Math.round(((row.leftAt || t) - row.joinedAt) / 1000),
    });
  }
  return { at: t / 1000, selfUserId: selfUserId, joined: joined, rows: rows };
};

/*
 * Per-user frame capture is NOT available in the Component View SDK.
 *
 * The design this bot was built to (see ZOOM_ATTENDANCE_BOT_PLAN.md) calls for
 * rendering one user's own stream to an off-screen canvas via
 * getMediaStream().renderVideo(). That method exists on the Video SDK client,
 * not on the Component View meeting client: createClient() returns an object
 * with no getMediaStream at all, in 3.13.2 and still in 6.2.0. The Video SDK
 * cannot join ordinary Zoom meetings, so there is no drop-in swap.
 *
 * Returning null keeps the capture loop honest: it records video_on from
 * Zoom's own per-user state, which is the signal the participation rule
 * actually needs, and simply stores no pixels. This used to throw and take the
 * entire join down with it.
 */
window.zoomCaptureSupported = async () => false;

window.zoomCaptureUser = async (userId) => null;

window.zoomLeave = async () => {
  // leaveMeeting(), not leave() — the latter does not exist and used to fail
  // silently inside a bare catch, so the bot never actually left.
  try {
    if (client && joined) {
      await client.leaveMeeting();
      joined = false;
    }
  } catch (e) { console.warn('leaveMeeting failed', e); }
};
