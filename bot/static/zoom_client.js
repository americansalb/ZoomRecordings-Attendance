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

// Shown in diagnostics so "is the deployed bot actually running this code"
// is answerable from the console instead of by archaeology on Render.
const PAGE_BUILD = 'capture-37: the page reports its own memory';

// How many tiles the gallery renders per page. Set by Python at join from
// BOT_GALLERY_TILES; 25 is Zoom's ceiling for any single participant, so a
// 50 person room is two alternating pages by Zoom's rules, not ours.
let galleryTilesWanted = 25;
// Lookout mode: presence, camera state, and chat only. Those all ride
// Zoom's signaling (the roster and user-added/updated/removed events), so
// the page keeps only a thumbnail-sized view alive and never loads the
// face detector. Video decode is what fills a small container; a lookout
// costs the same in a room of 5 or 250.
let lookoutMode = false;
// Seat watcher switch (BOT_SEAT_WATCHER, default on) and its live phase,
// so diagnostics can say exactly where it is instead of leaving a blank.
let seatWatcherEnabled = true;
let watcherPhase = 'not started';

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
  if (typeof u.bVideoOn === 'boolean') return u.bVideoOn;
  // Neither field present means the payload did not carry camera state at
  // all. That is "unknown", not "off": defaulting it to false would let a
  // partial update silently flip a live camera to off in our ledger.
  return null;
}

/*
 * Audit trail for camera state.
 *
 * When someone says "it thinks my camera is on and it is off", the dispute is
 * over which side failed: Zoom never sent the change, or we mishandled it.
 * This records every camera-state signal we receive, with its source, so the
 * diagnostics read settles that in one look instead of a redeploy.
 */
const videoEvents = [];
// Running totals, never capped: with the video rendering cut to one tile,
// these are the proof that Zoom's camera signal still arrives.
let cameraSignalTotal = 0;
let lastCameraSignalAt = null;
function recordVideoEvent(source, userId, name, value) {
  cameraSignalTotal += 1;
  lastCameraSignalAt = new Date().toISOString();
  videoEvents.push({
    at: new Date().toISOString(),
    source: source,               // 'user-added' | 'user-updated' | 'roster'
    userId: String(userId),
    name: name || '',
    videoOn: value,
  });
  if (videoEvents.length > 80) videoEvents.splice(0, videoEvents.length - 80);
}

// Chat receipts. "The SDK call resolved" and "Zoom actually distributed the
// message" are different facts: the send is recorded when attempted and when
// the SDK accepts it, and the SDK's own chat-on-message echo of our outbound
// message is recorded when it arrives. A send with no echo is a message Zoom
// swallowed, and without this log that is indistinguishable from delivered.
const chatLog = [];
function recordChat(kind, detail) {
  chatLog.push({ at: new Date().toISOString(), kind: kind, ...detail });
  if (chatLog.length > 60) chatLog.splice(0, chatLog.length - 60);
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
      // Nobody clicks in a headless browser, so the meeting must come up
      // already showing video. Gallery at full size makes the SDK attach a
      // <video-player node-id="<userId>"> per visible participant, which is
      // what frame capture screenshots, and it keeps the video pipeline
      // active so camera on and off state stays live instead of freezing at
      // whatever it was in a minimized view.
      customize: {
        video: {
          isResizable: false,
          defaultViewType: 'gallery',
          // Half the linear size, a quarter of the pixels of the old
          // 1280x720. This and maximumVideosInGalleryView below are the
          // only two numbers that control how much video this container
          // decodes, and they apply from the first frame at join, which
          // is precisely when a full room killed a 512 MB container.
          // A lookout shrinks further still: it never captures a frame,
          // so the view exists only to keep the SDK in a normal state,
          // and thumbnail-sized tiles are the cheapest normal there is.
          // A lookout reads cameras from Zoom's own signal (user-updated,
          // bVideoOn), which needs no decoded video at all. It renders ONE
          // thumbnail, as small as the SDK allows, in the normal gallery
          // view and never the minimized view: minimizing is what froze
          // the camera signal before, not the tile count. Measured live
          // 2026-09-05: four tiles at 320x180 cost Chrome 427 MB in a
          // 23-person class. This is the cut.
          viewSizes: lookoutMode ? {
            default: { width: 160, height: 90 },
            ribbon: { width: 160, height: 90 },
          } : {
            default: { width: 640, height: 360 },
            ribbon: { width: 640, height: 360 },
          },
        },
      },
      // The SDK decodes only the tiles on the visible page, and at a
      // 640x360 gallery a full 25 tile page is 128x72 per tile: Zoom
      // sends its smallest stream for each, so even a full page is a
      // modest decode. The count is configurable from the outside
      // (BOT_GALLERY_TILES) so it can be stepped down without a rebuild
      // if the memory meter on /healthz ever disagrees. A lookout pins
      // it at the floor: four thumbnails, a constant cost in any room.
      maximumVideosInGalleryView: lookoutMode ? 1 : galleryTilesWanted,
    });
  } catch (e) {
    client = null;               // let a retry re-init rather than reusing a dead client
    throw zoomError('Zoom SDK init failed', e);
  }

  // Inbound chat -> forward to the Python side (ignoring our own messages).
  client.on('chat-on-message', (payload) => {
    try {
      const sender = payload.sender || {};
      const receiver = payload.receiver || {};
      // Private if addressed to a specific user (i.e. a DM to the bot).
      const isPrivate = !!(receiver && receiver.userId);
      if (selfUserId && String(sender.userId) === String(selfUserId)) {
        // Zoom echoing back our own message is the delivery receipt.
        recordChat('echo', {
          to: receiver.name || String(receiver.userId || 'everyone'),
          toId: receiver.userId !== undefined ? String(receiver.userId) : null,
          preview: String(payload.message || '').slice(0, 80),
        });
        return;
      }
      recordChat('received', {
        from: sender.name || String(sender.userId || ''),
        preview: String(payload.message || '').slice(0, 80),
      });
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
        const on = participantVideoOn(u);
        if (on !== null) {
          recordVideoEvent('user-added', u.userId, participantName(u), on);
          settleVideo(row, on);
        }
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
        const on = participantVideoOn(u);
        if (on !== null) {
          recordVideoEvent('user-updated', u.userId, row.name, on);
          settleVideo(row, on);
        }
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
        // The reason rides along in words ("kicked by host", "ended by
        // host"): the control plane must tell a host removing the bot
        // from the meeting simply ending, and never walk back into the
        // first one.
        emitLifecycle('ended', payload.reason ? `${state}: ${payload.reason}` : String(state));
      }
    } catch (e) { console.error('connection-change handler error', e); }
  });

  return client;
}

window.zoomJoin = async (cfg) => {
  const wanted = parseInt(cfg.galleryTiles, 10);
  if (Number.isFinite(wanted)) galleryTilesWanted = Math.max(4, Math.min(25, wanted));
  seatWatcherEnabled = !/^(0|off|false|no)$/i.test(String(cfg.seatWatcher || 'on'));
  // A lookout never face-checks, so the detector must not load either: its
  // memory bite is exactly what this mode exists to avoid. The watcher
  // paths check lookoutMode themselves and say so in their phase, rather
  // than borrowing the BOT_SEAT_WATCHER switch and pointing whoever reads
  // diagnostics at an env var that is actually set to on.
  lookoutMode = !!cfg.lookout;
  cameraFaceWanted = !!cfg.cameraFace;
  cameraFace.reason = cfg.cameraFaceReason || null;
  cameraFaceMaxPeople = Number.isFinite(Number(cfg.cameraFaceMaxPeople))
    ? Math.max(0, Number(cfg.cameraFaceMaxPeople)) : 10;
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
  // While join() is pending the SDK may be sitting in the waiting room,
  // which looks identical to a slow connect from the outside. Watch the
  // SDK's own UI for the waiting room text and say so, both as a lifecycle
  // signal and in the eventual error if the host never admits the bot.
  let sawWaitingRoom = false;
  const waitingWatch = setInterval(() => {
    try {
      if (joined) { clearInterval(waitingWatch); return; }
      const text = (document.body && document.body.innerText) || '';
      if (/waiting room|host will let you in|meeting host will let you in|wait for the host/i.test(text)) {
        if (!sawWaitingRoom) {
          sawWaitingRoom = true;
          emitLifecycle('waiting_room', null);
        }
      }
    } catch (e) { /* the watcher must never break the join */ }
  }, 2000);
  try {
    await client.join(joinArgs);
  } catch (e) {
    let raw = '';
    try { raw = JSON.stringify(e) || String(e); } catch { raw = String(e); }
    if (/RECONNECTING_MEETING|OPERATION_CANCELLED/.test(raw)) {
      // Being admitted from the waiting room CANCELS the original join and
      // reconnects into the meeting. That is progress, not failure: the old
      // code threw here and tore the browser down at the exact moment the
      // host let the bot in. Wait for the reconnect to land instead.
      const t0 = Date.now();
      let connected = false;
      while (Date.now() - t0 < 120000) {
        try {
          const u = client.getCurrentUser && client.getCurrentUser();
          if (u && u.userId) { connected = true; break; }
        } catch (err) { /* not connected yet */ }
        await new Promise((r) => setTimeout(r, 1000));
      }
      if (!connected) {
        clearInterval(waitingWatch);
        throw zoomError('Zoom join failed: admission from the waiting room never completed', e);
      }
    } else {
      clearInterval(waitingWatch);
      if (sawWaitingRoom) {
        throw zoomError('Zoom join rejected: the bot reached the waiting room but was never admitted by the host', e);
      }
      throw zoomError('Zoom join rejected', e);
    }
  }
  clearInterval(waitingWatch);
  joined = true;
  startSeatWatcher();
  startCameraFace();
  startPageMemorySampler();

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
  const toId = (toUserId !== null && toUserId !== undefined && toUserId !== '')
    ? String(toUserId) : null;
  recordChat('send-attempt', { toId: toId || 'everyone',
    preview: String(text || '').slice(0, 80) });
  // sendChat(message, userId?) — omitting the id sends to everyone. There is
  // no getChatClient() on the Component View client and no sendToAll().
  try {
    let result;
    if (toId) {
      result = await client.sendChat(text, Number(toId));
    } else {
      result = await client.sendChat(text);
    }
    recordChat('sdk-accepted', { toId: toId || 'everyone' });
    return result;
  } catch (e) {
    recordChat('sdk-rejected', { toId: toId || 'everyone',
      error: String((e && (e.reason || e.message)) || e).slice(0, 200) });
    throw e;
  }
};

// Send and return whether delivery is proven. Proof, in order: the SDK
// resolving with the sent ChatMessage object (its documented return value,
// and the receipt this SDK actually provides: live testing showed
// chat-on-message does NOT echo the bot's own messages, so an echo-only
// scheme called visibly delivered messages failed); failing that, a short
// wait for an echo anyway, for SDK builds that resolve with nothing.
window.zoomSendChatConfirmed = async (text, toUserId, timeoutMs) => {
  const before = chatLog.length;
  const result = await window.zoomSendChat(text, toUserId);
  const toId = (toUserId !== null && toUserId !== undefined && toUserId !== '')
    ? String(toUserId) : null;
  if (result && typeof result === 'object' && !(result instanceof Error)) {
    recordChat('sdk-receipt', { toId: toId || 'everyone' });
    return { accepted: true, echoed: true, receipt: true };
  }
  const preview = String(text || '').slice(0, 80);
  const budget = Math.max(500, Math.min(4000, timeoutMs || 2000));
  const t0 = Date.now();
  while (Date.now() - t0 < budget) {
    const echoed = chatLog.slice(before).some((c) => c.kind === 'echo'
      && (c.preview === preview || (toId !== null && String(c.toId) === toId)));
    if (echoed) return { accepted: true, echoed: true };
    await new Promise((r) => setTimeout(r, 250));
  }
  return { accepted: true, echoed: false };
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
    // bHold is Zoom's waiting room flag. Someone on hold is outside the
    // meeting: not present, not observable, and never messageable.
    isHold: !!(u.bHold ?? u.isHold ?? false),
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
/*
 * The bot's camera picture.
 *
 * Zoom shows a profile photo only for a signed-in account, and this bot
 * joins as a guest, so the picture rides the camera instead: Python points
 * Chromium's fake webcam at a still image, and the bot switches its own
 * video on by pressing Zoom's Start Video button. The Component View has
 * no method for that, so the button is found by its accessible label.
 * Purely cosmetic: it never throws, it reports what it found through
 * diagnostics, and Python asks it to stop when memory gets tight.
 */
let cameraFaceWanted = false;
// Sending the picture into Zoom is a video encoder inside the browser:
// measured 2026-09-06, about 100 MB in a room of 23 (the box at 99
// percent wearing it, 79 without). The picture is a nicety for small
// rooms and never worth a freeze, so it stays off above this many people.
let cameraFaceMaxPeople = 10;
const cameraFace = {
  wanted: false, buttonFound: false, clicked: false, videoOn: null,
  phase: 'not started', error: null, clicks: 0, button: null, camera: null, notes: [],
};

function selfVideoOn() {
  try {
    const u = client && client.getCurrentUser && client.getCurrentUser();
    if (!u) return null;
    return !!(u.bVideoOn ?? u.video);
  } catch (e) { return null; }
}

// The whole routine lives in camera_face.js (window.CameraFace) so it can
// be tested on a plain page: it waits for Zoom's Start Video button to be
// pressable, presses it, and reports the button's label, its greyed-out
// state, the presses, a camera probe and Zoom's own words.
function roomSize() {
  try {
    const list = (client && client.getAttendeeslist && client.getAttendeeslist()) || [];
    return list.filter((u) => !u.isHold).length;
  } catch (e) { return 0; }
}

async function startCameraFace() {
  if (!cameraFaceWanted) return;
  if (!window.CameraFace) { cameraFace.wanted = true; cameraFace.phase = 'camera script not loaded'; return; }
  // Let the roster arrive, then size the room. A room already bigger
  // than the limit never gets the picture, whatever the memory meter
  // said before the join (it is always low then).
  await new Promise((r) => setTimeout(r, 4000));
  const people = roomSize();
  if (cameraFaceMaxPeople > 0 && people > cameraFaceMaxPeople) {
    cameraFace.wanted = false;
    cameraFace.videoOn = false;
    cameraFace.phase = `off: ${people} people in the room, the picture stays off above ${cameraFaceMaxPeople}`;
    cameraFace.reason = cameraFace.phase;
    return;
  }
  await window.CameraFace.start({
    client, root: document.getElementById('zoom-root') || document.body, state: cameraFace,
  });
}

window.zoomStopVideo = async () => {
  if (!window.CameraFace) return { ok: false, phase: 'camera script not loaded' };
  return window.CameraFace.stop({
    client, root: document.getElementById('zoom-root') || document.body, state: cameraFace,
  });
};

// The page's own memory, measured by the browser about once a minute
// (measureUserAgentSpecificMemory needs the cross-origin isolation this
// page already has). Read by /memz so growth can be placed: in the
// page's JavaScript and wasm, or outside it in the browser's decoders.
let pageMemory = null;
async function samplePageMemory() {
  try {
    if (performance.measureUserAgentSpecificMemory) {
      const m = await performance.measureUserAgentSpecificMemory();
      const byType = {};
      for (const b of m.breakdown || []) {
        const k = (b.types && b.types.join('+')) || 'other';
        byType[k] = (byType[k] || 0) + b.bytes;
      }
      pageMemory = {
        at: Date.now(), totalMB: +(m.bytes / 1048576).toFixed(1),
        byTypeMB: Object.fromEntries(Object.entries(byType).map(([k, v]) => [k, +(v / 1048576).toFixed(1)])),
      };
    } else if (performance.memory) {
      pageMemory = { at: Date.now(), jsHeapMB: +(performance.memory.usedJSHeapSize / 1048576).toFixed(1) };
    }
  } catch (e) { pageMemory = { at: Date.now(), error: String(e).slice(0, 80) }; }
}
let pageMemoryTimer = null;
function startPageMemorySampler() {
  if (pageMemoryTimer) return;
  samplePageMemory();
  pageMemoryTimer = setInterval(samplePageMemory, 60000);
}

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
  // The seat watcher's live report: its phase in plain words, which pixel
  // path proved readable, achieved checks per second, and per-seat state.
  try {
    out.watcher = window.SeatWatcher ? SeatWatcher.state() : { enabled: false };
    out.watcher.phase = watcherPhase;
  } catch (e) { out.watcher = { enabled: false, phase: watcherPhase, initState: 'state read failed' }; }
  // The camera-state signals received, newest last, and which tiles are
  // actually attached right now. Together these say whether a wrong camera
  // reading is Zoom never telling us, or us mishandling what it said.
  out.pageBuild = PAGE_BUILD;
  out.pageMemory = pageMemory;
  out.mediaElements = document.querySelectorAll('video, canvas').length;
  out.lookout = lookoutMode;
  out.cameraSignalTotal = cameraSignalTotal;
  out.lastCameraSignalAt = lastCameraSignalAt;
  out.cameraFace = { ...cameraFace };
  out.videoEvents = videoEvents.slice(-30);
  out.chatLog = chatLog.slice(-20);
  try { out.renderedVideoUsers = await window.zoomRenderedVideoUsers(); }
  catch (e) { out.renderedVideoUsers = []; }
  try { out.videoSurfaces = videoSurfaceInventory(); }
  catch (e) { out.videoSurfaces = []; }
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
      isHold: !!(u.bHold ?? u.isHold ?? false),
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
      if (live !== null && live !== row.videoOn) {
        recordVideoEvent('roster', u.userId, row.name, live);
        settleVideo(row, live, t);
      }
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
 * Per-user frame capture, from the SDK's own rendered tiles.
 *
 * The Component View has no getMediaStream().renderVideo() (that is Video SDK
 * API), so the original off-screen-canvas design cannot work here. What the
 * SDK does provide, verified in the 3.13.2 bundle: every tile it attaches
 * video to gets `node-id="<userId>"` set on the element, reset to "0" when
 * detached, with the self preview marked `media-type="preview"`. That is
 * Zoom's own binding of element to user id, so screenshotting that element is
 * attribution by identity, never by tile position.
 *
 * The screenshot itself happens on the Python side: Playwright captures the
 * compositor's output for the element, which includes WebGL, where
 * canvas.toDataURL() would return black. This side only finds and marks the
 * element.
 *
 * Coverage is whatever Zoom renders. A participant off the current gallery
 * page has no attached tile and returns not-found, and the caller records
 * that no check ran rather than inventing a result.
 */
function tilesForUser(userId) {
  const out = [];
  for (const el of document.querySelectorAll(`[node-id="${CSS.escape(String(userId))}"]`)) {
    if (el.getAttribute('media-type') === 'preview') continue;   // self preview
    const r = el.getBoundingClientRect();
    if (r.width < 16 || r.height < 16) continue;                 // detached or collapsed
    out.push({ el, area: r.width * r.height, rect: r });
  }
  // Largest wins: the same user can have a thumbnail and a main-stage tile.
  out.sort((a, b) => b.area - a.area);
  return out;
}

/*
 * Fallback for composite rendering.
 *
 * Some layouts draw every video into one shared canvas instead of per-user
 * elements, leaving nothing carrying a node-id. Attribution by tile position
 * is forbidden, but there is one case where attribution needs no positions at
 * all: exactly one participant besides the bot has a camera on and nobody is
 * screen sharing. Whatever video surface is being rendered IS that person, by
 * elimination. Any other configuration abstains.
 */
function singleCameraSurface(userId) {
  let users = null;
  try { users = client ? (client.getAttendeeslist() || []) : null; } catch (e) { users = null; }
  // Fixture pages have no SDK client; they provide the roster directly so the
  // fallback's logic is testable in a real browser.
  if (!users) users = window.__fixtureRoster || null;
  if (!users) return null;
  if (users.some((u) => u.sharerOn)) return null;
  const onCam = users.filter((u) => participantVideoOn(u) === true
    && String(u.userId) !== String(selfUserId));
  if (onCam.length !== 1 || String(onCam[0].userId) !== String(userId)) return null;

  const root = document.getElementById('zoom-root') || document.body;
  const surfaces = [...root.querySelectorAll('canvas, video, video-player')]
    .filter((el) => el.getAttribute('media-type') !== 'preview')
    .map((el) => ({ el, rect: el.getBoundingClientRect() }))
    .filter(({ rect }) => rect.width >= 100 && rect.height >= 60)
    .sort((a, b) => (b.rect.width * b.rect.height) - (a.rect.width * a.rect.height));
  return surfaces.length ? surfaces[0] : null;
}

// What video surfaces exist at all, biggest first. This is the evidence read:
// when capture finds nothing, this says whether the page has per-user tiles,
// one composite canvas, or nothing rendered, without guessing.
function videoSurfaceInventory() {
  return [...document.querySelectorAll('video-player, canvas, video')]
    .map((el) => {
      const r = el.getBoundingClientRect();
      return {
        tag: el.tagName.toLowerCase(),
        nodeId: el.getAttribute('node-id') || null,
        mediaType: el.getAttribute('media-type') || null,
        width: Math.round(r.width),
        height: Math.round(r.height),
      };
    })
    .filter((e) => e.width >= 8 && e.height >= 8)
    .sort((a, b) => (b.width * b.height) - (a.width * a.height))
    .slice(0, 30);
}

window.zoomVideoInventory = async () => videoSurfaceInventory();

/*
 * Start the continuous seat watcher over Zoom's own rendered tiles.
 * Fire-and-forget from the join: the watcher failing to start must never
 * fail a join, and its state (including why it is off, and which pixel
 * path proved readable) is reported through diagnostics either way.
 */
function liveTiles() {
  const seen = new Map();
  for (const el of document.querySelectorAll('[node-id]')) {
    if (el.getAttribute('media-type') === 'preview') continue;
    const id = el.getAttribute('node-id');
    if (!id || id === '0') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 16 || r.height < 16) continue;
    const area = r.width * r.height;
    const prev = seen.get(id);
    if (!prev || area > prev.area) seen.set(id, { id, el, area });
  }
  return [...seen.values()];
}

function startSeatWatcher() {
  // The detector LOADS here, at the join: the cheapest moment this
  // container will ever see, before the gallery has spun up its video
  // decoders. Waiting to load it until cameras appeared meant paying the
  // memory bite at the room's most expensive moment, and in real meetings
  // the meter never cleared the bar: the watcher sat installed and entire
  // sessions ended with zero face checks. Python still decides when to
  // START watching, which after this costs almost nothing.
  if (lookoutMode) { watcherPhase = 'off: lookout session, no video work'; return; }
  if (!window.SeatWatcher) { watcherPhase = 'engine not loaded'; return; }
  if (!seatWatcherEnabled) { watcherPhase = 'switched off by BOT_SEAT_WATCHER'; return; }
  watcherPhase = 'loading detector';
  SeatWatcher.init('/static/vendor/mediapipe/').then((ok) => {
    watcherPhase = ok
      ? 'ready: watching starts with the first rendered camera'
      : 'detector failed to load';
  }).catch((e) => {
    watcherPhase = 'failed: ' + String(e && e.message || e).slice(0, 120);
  });
}

window.zoomWatcherArm = async () => {
  try {
    if (lookoutMode) return { ok: false, phase: 'off: lookout session, no video work' };
    if (!window.SeatWatcher) return { ok: false, phase: 'engine not loaded' };
    if (!seatWatcherEnabled) return { ok: false, phase: watcherPhase };
    if (watcherPhase === 'running') return { ok: true, phase: 'running' };
    if (liveTiles().length === 0) return { ok: false, phase: watcherPhase };
    watcherPhase = 'loading detector';
    const ok = await SeatWatcher.init('/static/vendor/mediapipe/');
    if (!ok) { watcherPhase = 'detector failed to load'; return { ok: false, phase: watcherPhase }; }
    watcherPhase = 'running';
    SeatWatcher.start(liveTiles);
    return { ok: true, phase: watcherPhase };
  } catch (e) {
    watcherPhase = 'failed: ' + String(e && e.message || e).slice(0, 120);
    return { ok: false, phase: watcherPhase };
  }
};

window.zoomWatcherState = async () =>
  (window.SeatWatcher ? SeatWatcher.state() : { enabled: false, initState: 'not loaded' });

window.zoomMarkUserTile = async (userId) => {
  for (const el of document.querySelectorAll('[data-cap-target]')) {
    el.removeAttribute('data-cap-target');
  }
  const tiles = tilesForUser(userId);
  if (tiles.length === 0) {
    const single = singleCameraSurface(userId);
    if (single) {
      single.el.setAttribute('data-cap-target', '1');
      return {
        ok: true,
        width: Math.round(single.rect.width),
        height: Math.round(single.rect.height),
        tag: single.el.tagName.toLowerCase(),
        strategy: 'single-camera',
      };
    }
    // Say what IS rendered, so a persistent miss is diagnosable: wrong ids
    // and an empty gallery look identical from a bare null. Same filter as a
    // capture, so the preview never shows up as a capturable participant.
    const rendered = [...document.querySelectorAll('[node-id]')]
      .filter((el) => el.getAttribute('media-type') !== 'preview')
      .map((el) => el.getAttribute('node-id'))
      .filter((id) => id && id !== '0');
    return { ok: false, rendered: [...new Set(rendered)] };
  }
  const best = tiles[0];
  best.el.setAttribute('data-cap-target', '1');
  return {
    ok: true,
    width: Math.round(best.rect.width),
    height: Math.round(best.rect.height),
    tag: best.el.tagName.toLowerCase(),
    strategy: 'node-id',
  };
};

window.zoomUnmarkTile = async () => {
  for (const el of document.querySelectorAll('[data-cap-target]')) {
    el.removeAttribute('data-cap-target');
  }
};

window.zoomRenderedVideoUsers = async () => {
  const ids = [...document.querySelectorAll('[node-id]')]
    .filter((el) => el.getAttribute('media-type') !== 'preview')
    .map((el) => el.getAttribute('node-id'))
    .filter((id) => id && id !== '0');
  return [...new Set(ids)];
};

/*
 * Gallery paging: the "handful at a time" design.
 *
 * The browser decodes every tile the gallery currently shows, and only
 * those, so a small window showing one page at a time puts a constant
 * ceiling on decode work regardless of class size. What it costs is
 * coverage: people on other pages have no tile to capture. This walks the
 * SDK's own pagination one step per call, so over a few sweeps every page
 * gets its turn in front of the camera, and the capture path's honesty
 * rule already handles the rest: not rendered means recorded as not
 * checked, never guessed.
 *
 * The controls are found by their accessible labels rather than class
 * names, and when nothing matches, the visible button labels are returned
 * so the bot's-view diagnostics show exactly what was on offer.
 */
window.zoomGalleryAdvance = async () => {
  const root = document.getElementById('zoom-root') || document.body;
  const btns = [...root.querySelectorAll('button')];
  const label = (b) => `${b.getAttribute('aria-label') || ''} ${b.getAttribute('title') || ''} ${
    typeof b.className === 'string' ? b.className : ''}`;
  const usable = (b) => !b.disabled && b.getAttribute('aria-disabled') !== 'true'
    && b.offsetParent !== null;
  const next = btns.find((b) => /next[ -]?page|pagination[^a-z]*next|next[^a-z]*pagination/i.test(label(b)));
  const prev = btns.find((b) => /prev(ious)?[ -]?page|pagination[^a-z]*prev|prev[^a-z]*pagination/i.test(label(b)));
  if (next && usable(next)) { next.click(); return { ok: true, moved: 'next' }; }
  if (prev && usable(prev)) {
    // On the last page: walk back to the first so the cycle restarts.
    let guard = 12;
    while (guard-- > 0 && usable(prev)) {
      prev.click();
      await new Promise((r) => setTimeout(r, 120));
    }
    return { ok: true, moved: 'first' };
  }
  return {
    ok: false,
    buttons: btns.filter((b) => b.offsetParent !== null)
      .map((b) => label(b).trim().slice(0, 60)).filter(Boolean).slice(0, 12),
  };
};

window.zoomGalleryInfo = async () => {
  // How many pages the gallery spans right now, so the grid proctor knows
  // how fast to walk them to catch everyone inside the coverage window.
  // tilesPerPage is what the SDK was told to show (BOT_GALLERY_TILES),
  // which is also the decode budget: the smaller it is, the lighter each
  // page and the more pages a big class takes.
  try {
    const atts = (client && client.getAttendeeslist && client.getAttendeeslist()) || [];
    const participants = Math.max(1, atts.length);
    const per = Math.max(1, Math.min(galleryTilesWanted, 25));
    return { participants, tilesPerPage: per, pages: Math.max(1, Math.ceil(participants / per)) };
  } catch (e) {
    return { participants: 1, tilesPerPage: 25, pages: 1,
      error: String((e && e.message) || e).slice(0, 120) };
  }
};

window.zoomCaptureSupported = async () => true;

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
