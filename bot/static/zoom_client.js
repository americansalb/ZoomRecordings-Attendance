/*
 * INTEGRATION POINT — Zoom Web Meeting SDK (Component View) glue.
 *
 * Playwright (meeting_client.py) calls the window.zoom* functions below and
 * registers window.onZoomChat to receive inbound chat. This is the ONE file
 * that must be validated live against your Zoom SDK version — the method names
 * here target the Component View embedded client; confirm them against:
 *   https://developers.zoom.us/docs/meeting-sdk/web/component-view/
 *
 * Attribution is by Zoom user id: zoomCaptureUser(userId) renders THAT user's
 * own stream to our off-screen canvas, so tile position is irrelevant.
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
let stream = null;
let selfUserId = null;

async function ensureClient() {
  if (client) return client;
  client = ZoomMtgEmbedded.createClient();
  try {
    await client.init({
      zoomAppRoot: document.getElementById('zoom-root'),
      language: 'en-US',
      patchJsMedia: true,
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
  stream = client.getMediaStream();
  try {
    const me = client.getCurrentUser && client.getCurrentUser();
    selfUserId = me ? String(me.userId) : null;
  } catch (e) { selfUserId = null; }
};

window.zoomSendChat = async (text, toUserId) => {
  const chat = client.getChatClient();
  if (toUserId) {
    await chat.send(text, Number(toUserId));          // direct message
  } else if (chat.sendToAll) {
    await chat.sendToAll(text);                        // public, newer SDKs
  } else {
    await chat.send(text, 0);                          // public, 0 == everyone
  }
};

window.zoomListUsers = async () => {
  let users = [];
  if (client.getAttendeeslist) users = client.getAttendeeslist();
  else if (client.getAllUser) users = client.getAllUser();
  return (users || []).map((u) => ({
    userId: String(u.userId),
    displayName: u.displayName || u.userName || '',
    bVideoOn: !!u.bVideoOn,
    isHost: !!u.isHost,
  }));
};

window.zoomCaptureUser = async (userId) => {
  if (!stream) return null;
  const canvas = document.getElementById('cap-canvas');
  try {
    // Render this specific user's video into our own canvas, then grab a frame.
    await stream.renderVideo(canvas, Number(userId), canvas.width, canvas.height, 0, 0, 2);
    await new Promise((r) => setTimeout(r, 300)); // let a frame paint
    const url = canvas.toDataURL('image/png');
    try { await stream.stopRenderVideo(canvas, Number(userId)); } catch (e) {}
    return url;
  } catch (e) {
    console.warn('capture failed for', userId, e);
    return null;
  }
};

window.zoomLeave = async () => {
  try { if (client) await client.leave(); } catch (e) {}
};
