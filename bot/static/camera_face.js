/**
 * The bot's camera picture: switching it on through Zoom's own button,
 * and saying exactly what happened.
 *
 * Zoom's Component View has no method to start video; the footer button
 * is the only way in. That button (Zoom's class send-video-container__btn)
 * carries one of four labels:
 *   "start my video"   video is off and may be started
 *   "stop my video"    video is on
 *   "Video"            Zoom does not allow this participant to start video:
 *                      no camera device found in the browser, the camera
 *                      is blocked, or the host switched participant video
 *                      off
 *   "Video Disabled"   video is disabled for this participant
 * and it is greyed out (aria-disabled) until Zoom's video engine is ready,
 * which on a small machine can take a while after the join. Zoom ignores
 * a press on a greyed-out button, silently, which is how "pressed the
 * button, video never came on" happens with nothing to show for it.
 *
 * So: wait for the button to be enabled, press it, wait for Zoom to
 * confirm (the label flips to "stop my video" and the SDK's current user
 * reports bVideoOn), press again if it did not, and report the label, the
 * greyed-out state, the number of presses, a probe of the camera device
 * inside the browser, and any words Zoom put on screen. Purely cosmetic:
 * it never throws.
 *
 * Loaded before zoom_client.js; exposes window.CameraFace and nothing
 * else, so it runs unchanged on a plain page with a fake footer and a
 * fake client (see tests/camera_face_page.mjs).
 */
(function () {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const START_RE = /^\s*start\s*(my\s*)?video\s*$|turn\s*on\s*(my\s*)?video|unmute\s*video/i;
  const STOP_RE = /^\s*stop\s*(my\s*)?video\s*$|turn\s*off\s*(my\s*)?video|mute\s*video/i;
  const VIDEO_ONLY_RE = /^\s*video\s*$/i;
  const DISABLED_RE = /^\s*video\s*disabled\s*$/i;

  function labelOf(el) {
    return (el.getAttribute('aria-label') || el.getAttribute('title') || (el.textContent || ''))
      .trim().slice(0, 80);
  }

  function rootOf(root) {
    return root || document.getElementById('zoom-root') || document.body;
  }

  function findButton(root) {
    root = rootOf(root);
    const byClass = root.querySelector('.send-video-container__btn');
    if (byClass) return byClass;
    const all = [...root.querySelectorAll('button, [role="button"]')];
    const labelled = (re) => all.find((b) => re.test(labelOf(b)));
    return labelled(START_RE) || labelled(STOP_RE) || labelled(DISABLED_RE) || labelled(VIDEO_ONLY_RE) || null;
  }

  function isDisabled(el) {
    if (!el) return false;
    if (el.getAttribute('aria-disabled') === 'true' || el.disabled === true) return true;
    return /--disabled(\s|$)/.test(String(el.className || ''));
  }

  function buttonInfo(root) {
    const el = findButton(root);
    if (!el) return { label: null, disabled: null, visible: null, kind: 'missing' };
    const label = labelOf(el);
    let kind = 'other';
    if (START_RE.test(label)) kind = 'start';
    else if (STOP_RE.test(label)) kind = 'stop';
    else if (DISABLED_RE.test(label)) kind = 'disabled';
    else if (VIDEO_ONLY_RE.test(label)) kind = 'disallowed';
    return {
      label, kind, disabled: isDisabled(el), visible: el.offsetParent !== null,
      classes: String(el.className || '').slice(0, 120),
    };
  }

  function visibleLabels(root, limit) {
    root = rootOf(root);
    const out = [];
    for (const b of root.querySelectorAll('button, [role="button"], [role="menuitem"]')) {
      if (b.offsetParent === null) continue;
      const t = labelOf(b).slice(0, 40);
      if (t && !out.includes(t)) out.push(t);
      if (out.length >= limit) break;
    }
    return out;
  }

  // Toasts, alerts and dialogs Zoom put on the page: "Cannot start
  // video", a device picker, a permission ask. Zoom's words, not ours.
  function zoomWords(root) {
    root = root || document.body;
    const out = [];
    const sel = '[role="alert"], [role="alertdialog"], [role="dialog"], [class*="toast"], [class*="notification"]';
    for (const el of root.querySelectorAll(sel)) {
      const t = (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 140);
      if (t && !out.includes(t)) out.push(t);
      if (out.length >= 6) break;
    }
    return out;
  }

  // Is there a camera inside this browser at all, and does it deliver
  // frames? Run only when the picture did not come on, so the answer
  // sits next to the failure it explains.
  async function probeCamera() {
    const out = { devices: 0, labels: [], summary: 'unknown', error: null };
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
        out.summary = 'no media devices API in this browser';
        return out;
      }
      const devs = await navigator.mediaDevices.enumerateDevices();
      const cams = devs.filter((d) => d.kind === 'videoinput');
      out.devices = cams.length;
      out.labels = cams.map((d) => d.label || '(unnamed)').slice(0, 3);
      if (cams.length === 0) {
        out.summary = 'no camera device inside the browser (the fake webcam did not load)';
        return out;
      }
      const stream = await navigator.mediaDevices.getUserMedia({ video: true });
      const track = stream.getVideoTracks()[0];
      const s = track ? track.getSettings() : {};
      out.summary = `${cams.length} camera device${cams.length === 1 ? '' : 's'} (${out.labels.join(', ')}), test stream ${
        s.width || '?'}x${s.height || '?'} ${track ? track.readyState : 'no track'}`;
      stream.getTracks().forEach((t) => t.stop());
    } catch (e) {
      out.error = String((e && ((e.name ? e.name + ': ' : '') + e.message)) || e).slice(0, 120);
      out.summary = out.devices > 0
        ? `${out.devices} camera device(s), but opening one failed (${out.error})`
        : `camera probe failed (${out.error})`;
    }
    return out;
  }

  function videoOnFor(client) {
    try {
      const u = client && client.getCurrentUser && client.getCurrentUser();
      if (!u) return null;
      return !!(u.bVideoOn ?? u.video);
    } catch (e) { return null; }
  }

  async function start(opts) {
    const {
      client, state, deadlineMs = 90000, stepMs = 1500, clickGapMs = 12000, maxClicks = 4,
    } = opts || {};
    const root = rootOf(opts && opts.root);
    state.wanted = true;
    state.clicks = 0;
    state.strategies = [];
    state.notes = [];
    state.buttonFound = false;
    state.clicked = false;
    state.camera = null;
    state.phase = 'waiting for the meeting UI';
    const t0 = Date.now();
    let lastClick = 0;
    let moreTried = 0;
    try {
      // Strategy one, once: the client's own media stream, when the
      // runtime carries it (this SDK does not; kept for the day it does).
      try {
        const ms = client && typeof client.getMediaStream === 'function' ? client.getMediaStream() : null;
        if (ms && typeof ms.startVideo === 'function') {
          await Promise.race([
            ms.startVideo(),
            sleep(15000).then(() => { throw new Error('startVideo timed out'); }),
          ]);
          state.strategies.push({ strategy: 'media stream', tried: true, ok: true });
        } else {
          state.strategies.push({ strategy: 'media stream', tried: false });
        }
      } catch (e) {
        state.strategies.push({ strategy: 'media stream', tried: true, ok: false,
          error: String((e && (e.reason || e.message)) || e).slice(0, 120) });
      }

      while (Date.now() - t0 < deadlineMs) {
        if (videoOnFor(client) === true) { state.videoOn = true; state.phase = 'video on'; return state; }
        const info = buttonInfo(root);
        state.button = info;
        state.toolbar = visibleLabels(root, 14);
        if (info.kind === 'stop') {
          state.videoOn = true;
          state.phase = 'video on (Zoom shows Stop Video)';
          return state;
        }
        if (info.kind === 'disallowed') {
          state.phase = 'Zoom does not allow this participant to start video (its button just says "Video"): '
            + 'no camera found in the browser, the camera is blocked, or the host switched participant video off';
          break;
        }
        if (info.kind === 'disabled') {
          state.phase = 'Zoom has video disabled for this participant (its button says "Video Disabled")';
          break;
        }
        if (info.kind === 'missing') {
          // A small window folds the footer into "More"; look there.
          if (Date.now() - lastClick > clickGapMs && moreTried < 3) {
            moreTried += 1;
            const more = [...root.querySelectorAll('button, [role="button"]')]
              .find((b) => /\bmore\b/i.test(labelOf(b)) && b.offsetParent !== null);
            if (more) {
              more.click();
              await sleep(700);
              const item = [...document.querySelectorAll('button, [role="button"], [role="menuitem"], li, a')]
                .find((b) => START_RE.test(labelOf(b)) && b.offsetParent !== null);
              if (item) {
                item.click();
                state.clicks += 1; state.clicked = true; state.buttonFound = true; lastClick = Date.now();
                state.phase = 'pressed Start Video in the More menu, waiting for Zoom to confirm';
              } else {
                state.phase = 'no Start Video control on the toolbar or in the More menu';
              }
            } else {
              state.phase = 'no Start Video control on the toolbar and no More menu';
            }
          }
        } else if (info.disabled) {
          state.phase = "Zoom's Start Video button is greyed out (its video engine is not ready), waiting";
        } else {
          state.buttonFound = true;
          if (state.clicks >= maxClicks && Date.now() - lastClick > clickGapMs) break;
          if (Date.now() - lastClick > clickGapMs && state.clicks < maxClicks) {
            try { findButton(root).click(); } catch (e) { /* the phase reports the outcome */ }
            state.clicks += 1; state.clicked = true; lastClick = Date.now();
            state.phase = `pressed Start Video (${state.clicks}), waiting for Zoom to confirm`;
          }
        }
        await sleep(stepMs);
      }

      state.videoOn = videoOnFor(client);
      if (state.videoOn === true) { state.phase = 'video on'; return state; }
      state.camera = await probeCamera();
      state.notes = zoomWords(document.body);
      if (!/does not allow|has video disabled/.test(state.phase)) {
        const b = state.button || {};
        const seconds = Math.round((Date.now() - t0) / 1000);
        const why = b.kind === 'missing' ? 'no Start Video control found'
          : b.disabled ? `the button stayed greyed out for ${seconds} seconds`
            : state.clicks > 0 ? `pressed ${state.clicks} ${state.clicks === 1 ? 'time' : 'times'}, Zoom never switched it on`
              : `button "${b.label}" never became pressable`;
        state.phase = `Zoom never reported video on (${why})`;
      }
      return state;
    } catch (e) {
      state.error = String((e && e.message) || e).slice(0, 160);
      state.phase = 'failed';
      return state;
    }
  }

  async function stop(opts) {
    const { client, state } = opts || {};
    const root = rootOf(opts && opts.root);
    try {
      if (videoOnFor(client) === false) return { ok: true, phase: 'already off' };
      const info = buttonInfo(root);
      if (info.kind === 'missing') return { ok: false, phase: 'no video button found' };
      if (info.kind !== 'stop') return { ok: true, phase: 'already off' };
      findButton(root).click();
      if (state) { state.phase = 'switched off to make room'; state.videoOn = false; }
      return { ok: true };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e).slice(0, 160) };
    }
  }

  window.CameraFace = { start, stop, buttonInfo, probeCamera, visibleLabels, zoomWords, findButton };
})();
