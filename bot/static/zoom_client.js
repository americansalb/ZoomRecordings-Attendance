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
const PAGE_BUILD = 'capture-66: a breakout room test that says whether this seat can make, open and close rooms';

// The most tiles the gallery may put on a page. Set by Python at join from
// BOT_GALLERY_TILES; 25 is Zoom's ceiling for any single participant. It
// is a ceiling, not the page size: Zoom sizes a page from the room it
// has (a tile needs 240 pixels across and 136.75 down, read out of the
// 6.2.0 kit), so the bot's 640 by 365 gallery holds two rows of two, four
// tiles, one of them the bot's own picture, whatever this says.
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
  // A room that gives everyone their own link (a registered meeting)
  // refuses a join without the registrant's token: "This meeting requires
  // registration". The control plane registers the bot and sends its
  // token; Zoom also checks the email it was registered under. Only set
  // when present, so every other room joins exactly as before.
  if (cfg.tk) joinArgs.tk = cfg.tk;
  if (cfg.tk && cfg.userEmail) joinArgs.userEmail = cfg.userEmail;
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
  startBreakoutWatch();

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
    const why = String((e && (e.reason || e.message)) || e).slice(0, 200);
    recordChat('sdk-rejected', { toId: toId || 'everyone', error: why });
    // This SDK rejects with a bare object, which reached the console as
    // "send failed: Page.evaluate: Object" and told nobody anything. The
    // usual cause is a connection Zoom has retired, so say which it is:
    // the roster is right here and settles it.
    if (toId) {
      let onRoster = false;
      try {
        onRoster = (client.getAttendeeslist() || [])
          .some((u) => String(u.userId) === toId);
      } catch (e2) { onRoster = true; }   // unreadable: do not claim they left
      if (!onRoster) {
        throw new Error('that person is no longer in the meeting under that id');
      }
    }
    throw new Error(`Zoom refused the message: ${why === '[object Object]' ? 'no reason given' : why}`);
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
    // Still kept: a room that empties below the cap gets the picture.
    keepCameraFace();
    return;
  }
  await window.CameraFace.start({
    client, root: document.getElementById('zoom-root') || document.body, state: cameraFace,
  });
  keepCameraFace();
}

// The picture is kept on for the whole meeting, not only tried at the
// join (CameraFace.keep). It stays off once Python switched it off to
// make room, while the room is over the headcount cap, and while the bot
// is not connected.
let cameraFaceStoppedForMemory = false;
let cameraFaceKeeper = null;
function keepCameraFace() {
  if (cameraFaceKeeper || !window.CameraFace || !window.CameraFace.keep) return;
  cameraFaceKeeper = window.CameraFace.keep({
    client, root: document.getElementById('zoom-root') || document.body, state: cameraFace,
    allowed: () => {
      if (!cameraFaceWanted) return 'not wanted for this session';
      if (cameraFaceStoppedForMemory) return 'switched off to make room, staying off';
      if (!joined) return 'not connected to the meeting';
      const people = roomSize();
      if (cameraFaceMaxPeople > 0 && people > cameraFaceMaxPeople) {
        return `${people} people in the room, the picture stays off above ${cameraFaceMaxPeople}`;
      }
      return true;
    },
  });
}

window.zoomStopVideo = async () => {
  // Python asks for this at the memory limit: the keeper must not put
  // the picture back.
  cameraFaceStoppedForMemory = true;
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
  // Where the bot is sitting: the main room, or a breakout room it is
  // trying to walk out of. The console shows this when a headcount
  // suddenly drops, so nobody has to guess whether a class left.
  out.breakout = { ...readBreakout(), events: breakout.events.slice(-10) };
  // The gallery's pages: which one is on screen, how many there are, and
  // the last turns with their outcome. A turn that failed says why.
  try {
    out.gallery = { paged: hasPagination(), page: gallery.page, pages: gallery.pages,
      turns: gallery.turns, failures: gallery.failures, recent: gallery.recent.slice(-6) };
  } catch (e) { out.gallery = { error: String(e).slice(0, 120) }; }
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
/*
 * Breakout rooms.
 *
 * The bot's place is the main room, because that is the only seat that
 * sees the whole class. A host can move any participant into a room with
 * one click, and until this existed the bot had no way back: on 2026-09-11
 * it sat in a room counting four people while the class carried on
 * without it, and nobody could get it out.
 *
 * Two jobs, and deliberately only two.
 *
 *   1. If the bot ends up in a room, walk out. leaveBreakoutRoom() is on
 *      the same connection every other call here uses; it was verified
 *      present on the 6.2.0 build this image vendors before this was
 *      written. If the host has the bot assigned to a room with automatic
 *      joining on, walking out is a fight that cannot be won, so it stops
 *      after a few tries and says so rather than bouncing all class.
 *
 *   2. Say whether rooms are OPEN at all. When they are, the main room
 *      empties, and that is not a class leaving: presence has to be held,
 *      nothing counted against anybody, nobody messaged.
 *
 * What this does NOT try to answer is who is in which room.
 * getBreakoutRoomList gives a HOST every room and a participant only its
 * own, and the bot is never the host. That belongs to Zoom's own
 * announcements, not to a guess made from this seat.
 */
const BO_WATCH_MS = 5000;
// Enough tries to beat a host who moved the bot by hand, few enough that
// automatic assignment does not become an all-class tug of war.
const BO_LEAVE_ATTEMPTS = 5;
// Once the bot has been back in the main session this long, a later
// crossing is a fresh event and gets its own tries.
const BO_ATTEMPT_RESET_MS = 120000;

const breakout = {
  roomsOpen: false,      // Zoom says rooms are open
  botInRoom: false,      // and the bot is inside one
  status: null,          // Zoom's own room status, unchanged
  userStatus: null,      // Zoom's own word for where this participant is
  roomName: null,
  attempts: 0,
  lastAttemptAt: null,
  lastMainSessionAt: null,
  gaveUp: false,
  supported: null,       // whether this SDK offers the calls at all
  events: []
};

function boRecord(kind, detail) {
  breakout.events.push({ at: new Date().toISOString(), kind: kind, detail: detail || null });
  if (breakout.events.length > 40) breakout.events.splice(0, breakout.events.length - 40);
}

// Zoom's own statuses, read as words rather than numbers where it offers
// words: 2 is "in progress" for a room, and a participant reports "in room"
// or "joining" while they are in one.
const BO_OPEN_STATUSES = [2, 3];
const BO_INSIDE = ['in room', 'joining'];

function readBreakout() {
  if (!client || typeof client.getBreakoutRoomStatus !== 'function') {
    breakout.supported = false;
    return breakout;
  }
  breakout.supported = true;
  try {
    const status = client.getBreakoutRoomStatus();
    const userStatus = typeof client.getUserStatus === 'function' ? client.getUserStatus() : null;
    breakout.status = status === undefined ? null : status;
    breakout.userStatus = userStatus === undefined ? null : userStatus;
    breakout.roomsOpen = BO_OPEN_STATUSES.includes(Number(status));
    breakout.botInRoom = BO_INSIDE.includes(String(userStatus || '').toLowerCase());
    if (!breakout.botInRoom) breakout.lastMainSessionAt = nowMs();
    if (breakout.botInRoom && typeof client.getCurrentBreakoutRoom === 'function') {
      const room = client.getCurrentBreakoutRoom() || {};
      breakout.roomName = room.name || room.roomName || null;
    } else {
      breakout.roomName = null;
    }
  } catch (e) {
    // A room state we cannot read is not a room we are in. Guessing the
    // other way would retire a whole class from the record.
    boRecord('read-failed', String((e && e.message) || e).slice(0, 200));
  }
  return breakout;
}

async function boTick() {
  const state = readBreakout();
  if (!state.supported) return;
  if (!state.botInRoom) {
    if (state.lastMainSessionAt && state.attempts > 0
        && nowMs() - state.lastAttemptAt > BO_ATTEMPT_RESET_MS) {
      state.attempts = 0;
      state.gaveUp = false;
    }
    return;
  }
  if (state.attempts >= BO_LEAVE_ATTEMPTS) {
    if (!state.gaveUp) {
      state.gaveUp = true;
      boRecord('gave-up', 'the host has this bot assigned to a room');
    }
    return;
  }
  state.attempts += 1;
  state.lastAttemptAt = nowMs();
  boRecord('leaving', { attempt: state.attempts, room: state.roomName });
  try {
    await client.leaveBreakoutRoom();
    boRecord('left', { attempt: state.attempts });
  } catch (e) {
    boRecord('leave-failed', String((e && (e.reason || e.message)) || e).slice(0, 200));
  }
}

let boTimer = null;
function startBreakoutWatch() {
  if (boTimer) return;
  readBreakout();
  boTimer = setInterval(() => { boTick().catch(() => {}); }, BO_WATCH_MS);
}

window.zoomBreakout = async () => (readBreakout(), {
  supported: breakout.supported,
  roomsOpen: breakout.roomsOpen,
  botInRoom: breakout.botInRoom,
  room: breakout.roomName,
  status: breakout.status,
  userStatus: breakout.userStatus,
  attempts: breakout.attempts,
  gaveUp: breakout.gaveUp,
  events: breakout.events.slice(-10)
});

/*
 * The breakout room test (2026-11-21). Can this seat run a class's rooms?
 * Only a host or co-host can, and the bot has never been either, so this
 * answers three things in an empty test meeting and changes nothing in a
 * class: what role Zoom gave this seat, whether rooms can be made from it,
 * and whether they open and close. Every step is written down with Zoom's
 * own answer, so a refusal says which step refused and why.
 */
function boErr(e) {
  if (!e) return 'unknown';
  if (typeof e === 'string') return e.slice(0, 300);
  const parts = [e.reason, e.message, e.type, e.errorCode].filter((x) => x !== undefined && x !== null);
  return (parts.length ? parts.join(' | ') : JSON.stringify(e)).slice(0, 300);
}

// The Component View draws its own host panel, and the panel's code holds
// the call that makes rooms (the public client offers only the one that
// opens rooms already made). Walk React's own record of the drawn page to
// find that object. Best effort: if it is not found, the test says so.
function findRoomMaker() {
  const seen = new Set();
  const looks = (v) => v && typeof v === 'object' && typeof v.createRooms === 'function'
    && typeof v.openRooms === 'function';
  const fromValue = (v, depth) => {
    if (!v || typeof v !== 'object' || depth > 3 || seen.has(v)) return null;
    seen.add(v);
    if (looks(v)) return v;
    if (depth < 3) {
      for (const k of Object.keys(v).slice(0, 60)) {
        let x = null;
        try { x = fromValue(v[k], depth + 1); } catch (e) { x = null; }
        if (x) return x;
      }
    }
    return null;
  };
  const nodes = document.querySelectorAll('#meetingSDKElement *, #zmmtg-root *, body *');
  for (let i = 0; i < nodes.length && i < 4000; i += 1) {
    const el = nodes[i];
    const key = Object.keys(el).find((k) => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
    if (!key) continue;
    let f = el[key];
    for (let up = 0; f && up < 80; up += 1, f = f.return) {
      let ctx = f.dependencies && f.dependencies.firstContext;
      for (let n = 0; ctx && n < 20; n += 1, ctx = ctx.next) {
        const hit = fromValue(ctx.memoizedValue, 0);
        if (hit) return hit;
      }
      const hit = fromValue(f.memoizedProps && f.memoizedProps.value, 0);
      if (hit) return hit;
    }
  }
  return null;
}

window.zoomBreakoutTest = async (opts) => {
  const o = opts || {};
  const names = Array.isArray(o.names) && o.names.length ? o.names.slice(0, 4) : ['Test room 1', 'Test room 2'];
  const openSeconds = Math.max(3, Math.min(60, Number(o.openSeconds) || 15));
  const steps = [];
  const step = (name, ok, detail) => steps.push({ step: name, ok: !!ok, detail: detail === undefined ? null : detail });
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const out = { at: new Date().toISOString(), steps };
  if (!client) { step('client', false, 'no client'); return out; }

  // From a host seat: make the named person a co-host, the way a trainer
  // would by hand, and stop there.
  if (o.makeCoHost) {
    try {
      const want = String(o.makeCoHost).toLowerCase();
      const users = client.getAttendeeslist() || [];
      const who = users.find((u) => String(u.displayName || u.userName || '').toLowerCase().includes(want));
      if (!who) { step('find the person to make co-host', false, users.map((u) => u.displayName)); return out; }
      await client.makeCoHost(who.userId);
      step('make co-host', true, who.displayName);
    } catch (e) { step('make co-host', false, boErr(e)); }
    out.done = 'finished';
    return out;
  }

  try {
    const me = client.getCurrentUser && client.getCurrentUser();
    out.role = {
      isHost: typeof client.isHost === 'function' ? !!client.isHost() : null,
      isCoHost: typeof client.isCoHost === 'function' ? !!client.isCoHost() : null,
      isOriginHost: typeof client.isOriginHost === 'function' ? !!client.isOriginHost() : null,
      userRole: me ? (me.userRole ?? null) : null,
      isGuest: me ? (me.isGuest ?? null) : null,
    };
    step('role', true, out.role);
  } catch (e) { step('role', false, boErr(e)); }

  let rooms = [];
  try {
    rooms = (client.getBreakoutRoomList && client.getBreakoutRoomList()) || [];
    step('rooms already made', true, rooms.map((r) => ({ name: r.name, id: !!r.roomId })));
  } catch (e) { step('rooms already made', false, boErr(e)); }

  if (!rooms.length) {
    let maker = null;
    try { maker = findRoomMaker(); } catch (e) { maker = null; }
    step('found the call that makes rooms', !!maker, maker ? null : 'not found on the page');
    if (maker) {
      try {
        const made = await maker.createRooms(names, 1);
        rooms = Array.isArray(made) ? made : [];
        step('make rooms', rooms.length > 0, rooms.map((r) => r.name));
      } catch (e) { step('make rooms', false, boErr(e)); }
    }
  }
  if (!rooms.length) { out.done = 'no rooms to open'; return out; }

  try {
    const list = rooms.map((r) => ({ roomId: r.roomId, name: r.name, attendeeList: [] }));
    await client.openBreakoutRooms(list, { isAutoJoinRoom: false, isBackToMainSessionEnabled: true, waitSeconds: 0 });
    step('open rooms', true, list.map((r) => r.name));
  } catch (e) { step('open rooms', false, boErr(e)); out.done = 'could not open'; return out; }

  await wait(4000);
  try { step('status while open', true, { status: client.getBreakoutRoomStatus() }); }
  catch (e) { step('status while open', false, boErr(e)); }
  await wait(Math.max(0, openSeconds * 1000 - 4000));

  try { await client.closeAllBreakoutRooms(); step('close rooms', true); }
  catch (e) { step('close rooms', false, boErr(e)); }
  for (let i = 0; i < 20; i += 1) {
    await wait(1000);
    let st = null;
    try { st = client.getBreakoutRoomStatus(); } catch (e) { st = null; }
    if (Number(st) !== 2 && Number(st) !== 3) { step('status after closing', true, { status: st }); break; }
    if (i === 19) step('status after closing', false, { status: st });
  }
  out.done = 'finished';
  return out;
};

window.zoomPresence = async () => {
  const t = nowMs();
  const rows = [];
  // Reconcile against the live roster first. Events are the primary signal but
  // they can be missed (a dropped websocket frame, a handler that threw), and
  // a roster read is cheap insurance against a participant who is present but
  // absent from our ledger.
  // Our own id changes with everybody else's on a breakout crossing, so
  // it is re-read here rather than trusted from the join. Left stale it
  // silently disabled the cleanup below, which reads the roster for the
  // bot itself before believing it.
  try {
    const me = client.getCurrentUser && client.getCurrentUser();
    if (me && me.userId !== undefined && me.userId !== null) {
      selfUserId = String(me.userId);
    }
  } catch (e) { /* keep the last id we knew */ }

  const seen = new Set();
  let rosterTrusted = false;
  try {
    const list = client.getAttendeeslist() || [];
    for (const u of list) {
      seen.add(String(u.userId));
      const row = ledgerFor(u.userId, participantName(u));
      if (row.leftAt !== null) row.leftAt = null;   // rejoined
      row.missed = 0;
      const live = participantVideoOn(u);
      if (live !== null && live !== row.videoOn) {
        recordVideoEvent('roster', u.userId, row.name, live);
        settleVideo(row, live, t);
      }
    }
    // A roster without us in it is an SDK mid-reconnect, not an empty
    // room. Trusting it would retire the entire class from the record.
    rosterTrusted = list.length > 0
      && (selfUserId === null || seen.has(String(selfUserId)));
  } catch (e) { console.warn('presence roster read failed', e); }

  // Close the connections Zoom has stopped listing.
  //
  // user-removed fires when somebody leaves, but it does NOT fire when
  // the ROOM changes underneath us: crossing into a breakout room and
  // back re-keys everybody, and their old connections simply stop
  // appearing. Nothing retired them, so they stayed open, frozen, and a
  // class of 18 reached the console as 53 people (2026-09-11).
  //
  // Held, never retired, while rooms are open or while the bot is in one:
  // then an empty main room means the class is in the rooms, and marking
  // them gone would be the worse mistake by far.
  const bo = readBreakout();
  const holdEveryone = bo.botInRoom || bo.roomsOpen;
  for (const row of presence.values()) {
    if (row.leftAt !== null) continue;
    if (seen.has(row.userId)) continue;
    if (!rosterTrusted || holdEveryone) { row.missed = 0; continue; }
    row.missed = (row.missed || 0) + 1;
    // Two reads running, so one missed frame never retires anybody.
    if (row.missed >= 2) {
      settleVideo(row, false, t);
      row.leftAt = t;
      recordVideoEvent('roster-gone', row.userId, row.name, false);
    }
  }

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
  return {
    at: t / 1000, selfUserId: selfUserId, joined: joined, rows: rows,
    // Rooms open means the main room is not the class. Whoever reads this
    // holds the stretch rather than counting it against anybody.
    breakout: {
      supported: breakout.supported, roomsOpen: breakout.roomsOpen,
      botInRoom: breakout.botInRoom, room: breakout.roomName,
      gaveUp: breakout.gaveUp
    }
  };
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
 * Gallery paging: one page on screen at a time, every page in its turn.
 *
 * The browser decodes only the tiles on the page the gallery shows, so the
 * bot shows one page at a time and turns through them: decode work stays
 * the same whatever the class size. Only a tile on screen can have its
 * face checked, so whether every student gets checked comes down to this
 * turning, and nothing else.
 *
 * FOUND 2026-09-25 (owner: face checks were "not even between
 * participants"): this used to look for buttons labelled "next page" or
 * "previous page". Zoom's are labelled "Increase page" and "Decrease page",
 * read out of both kits the bot has run (3.13.2 and 6.2.0), so not one
 * page was ever turned. The answer was thrown away, so nothing said so,
 * and every face check, room picture and student photo came from the first
 * page: at the bot's size four tiles, one of them the bot. Zoom's own
 * words come first now, the older patterns after them.
 *
 * Zoom hides the arrow that cannot be used with visibility:hidden, never
 * display:none, so whether one can be pressed is read from its computed
 * style (offsetParent says yes to a hidden button). Zoom renders no page
 * buttons at all while everybody fits on one page, which is a gallery
 * with nothing to turn, not a fault.
 */
const PAGE_WORDS = {
  next: [/^\s*increase page\s*$/i, /next[ -]?page/i],
  prev: [/^\s*decrease page\s*$/i, /prev(ious)?[ -]?page/i],
};
// The old matcher also read class names; kept as the last resort.
const PAGE_CLASS_WORDS = {
  next: /pagination[^a-z]*next|next[^a-z]*pagination/i,
  prev: /pagination[^a-z]*prev|prev[^a-z]*pagination/i,
};
const gallery = { page: 0, pages: null, turns: 0, failures: 0, last: null, recent: [] };

function pageButtons() {
  const root = document.getElementById('zoom-root') || document.body;
  return [...root.querySelectorAll('button, [role="button"]')];
}

function pageButton(which, btns = pageButtons()) {
  const words = (b) => `${b.getAttribute('aria-label') || ''} ${b.getAttribute('title') || ''}`.trim();
  for (const re of PAGE_WORDS[which]) {
    const hit = btns.find((b) => re.test(words(b)));
    if (hit) return hit;
  }
  return btns.find((b) => PAGE_CLASS_WORDS[which].test(
    typeof b.className === 'string' ? b.className : '')) || null;
}

function pressable(b) {
  if (!b || b.disabled || b.getAttribute('aria-disabled') === 'true') return false;
  const cs = getComputedStyle(b);
  if (cs.visibility === 'hidden' || cs.display === 'none') return false;
  return b.getClientRects().length > 0;
}

function hasPagination() {
  return !!(pageButton('next') || pageButton('prev')
    || document.querySelector('[aria-label="Pagination"]'));
}

function noteTurn(entry) {
  gallery.last = { at: Date.now(), ...entry };
  gallery.recent.push(gallery.last);
  gallery.recent.splice(0, Math.max(0, gallery.recent.length - 12));
  if (entry.ok) gallery.turns += 1; else gallery.failures += 1;
}

const settle = (ms) => new Promise((r) => setTimeout(r, ms));

/*
 * Turn the gallery. direction 'next' moves one page on, and from the last
 * page goes back to the first; 'first' goes back to the first page. The
 * answer always says what happened, and a turn that could not happen says
 * why, with the buttons that were on the page, so a failure is never
 * silent again.
 */
window.zoomGalleryTurn = async (direction = 'next') => {
  const btns = pageButtons();
  const next = pageButton('next', btns);
  const prev = pageButton('prev', btns);
  if (!next && !prev) {
    if (!document.querySelector('[aria-label="Pagination"]')) {
      gallery.page = 0;
      gallery.pages = 1;
      return { ok: true, moved: 'none', page: 0, pages: 1 };
    }
    const offered = btns.filter((b) => b.getClientRects().length > 0)
      .map((b) => `${b.getAttribute('aria-label') || ''} ${b.getAttribute('title') || ''}`.trim())
      .filter(Boolean).slice(0, 12);
    const why = 'the gallery has more than one page but no page buttons were found';
    noteTurn({ ok: false, why, buttons: offered });
    return { ok: false, why, buttons: offered };
  }
  // Zoom hides "Decrease page" on the first page, which settles where we
  // are whatever the count says (a change of view resets Zoom to page one).
  if (!pressable(prev)) gallery.page = 0;
  if (direction !== 'first' && pressable(next)) {
    next.click();
    gallery.page += 1;
    await settle(60);
    if (!pressable(pageButton('next'))) gallery.pages = gallery.page + 1;
    noteTurn({ ok: true, moved: 'next', page: gallery.page });
    return { ok: true, moved: 'next', page: gallery.page, pages: gallery.pages };
  }
  // Back to the first page. Zoom has no jump, so the other arrow is pressed
  // until it hides, which is what happens on the first page. The presses it
  // took say which page was on screen, whatever the count kept here said
  // (Zoom moves the gallery by itself when people leave), so from the last
  // page they are also how many pages there are.
  const fromLast = direction !== 'first' && !pressable(next);
  let presses = 0;
  for (let guard = 0; guard < 60 && pressable(pageButton('prev')); guard += 1) {
    pageButton('prev').click();
    presses += 1;
    await settle(40);
  }
  if (pressable(pageButton('prev'))) {
    const why = 'could not get back to the first page';
    noteTurn({ ok: false, why, presses });
    return { ok: false, why, presses };
  }
  if (fromLast) gallery.pages = presses + 1;
  gallery.page = 0;
  noteTurn({ ok: true, moved: 'first', presses });
  return { ok: true, moved: 'first', page: 0, pages: gallery.pages, presses };
};

// Kept for anything still calling the old name: one page on, or back to
// the first from the last.
window.zoomGalleryAdvance = async () => window.zoomGalleryTurn('next');

window.zoomGalleryInfo = async () => {
  // Where the gallery stands: whether it has pages at all, which page is
  // on screen, how many pages the bot has found by walking them (null
  // until it has reached the last one), and every turn it has made.
  let participants = 1;
  try {
    const atts = (client && client.getAttendeeslist && client.getAttendeeslist()) || [];
    participants = Math.max(1, atts.length);
  } catch (e) { /* the count is a nicety; the page state below is the point */ }
  const prev = pageButton('prev');
  const paged = hasPagination();
  if (paged && !pressable(prev)) gallery.page = 0;
  return {
    participants,
    paged,
    page: paged ? gallery.page : 0,
    pages: paged ? gallery.pages : 1,
    canNext: pressable(pageButton('next')),
    turns: gallery.turns,
    failures: gallery.failures,
    last: gallery.last,
  };
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
