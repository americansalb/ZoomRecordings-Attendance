/*
 * SeatWatcher: continuous in-page face watching.
 *
 * The old pipeline photographed one tile at a time and mailed each photo to
 * Python (a Polaroid through a mail slot, roughly one per second TOTAL).
 * This engine runs face detection inside the same browser that is already
 * decoding the video, so the cost per check is milliseconds and every
 * rendered seat can be checked about once a second.
 *
 * Deliberately Zoom-agnostic: it takes a provider function that returns
 * [{id, el}] tiles, so a test page can drive it with fake tiles. Zoom's
 * rendered tiles are canvases that may refuse direct pixel reads (WebGL
 * compositing reads back black), which is the exact reason the Polaroid
 * design existed. So this engine PROBES pixel paths per tile and records
 * which one works: a contained <video>, a direct canvas draw, or
 * captureStream (which captures composited frames even where direct reads
 * return black). The probe verdict is surfaced in state() so the first
 * real meeting answers the open question instead of anyone guessing.
 *
 * Failure posture: everything is best-effort. If the detector library is
 * missing or no pixel path works, state() says so and the rest of the bot
 * is untouched.
 */
(function () {
  const work = document.createElement('canvas');
  work.width = 192; work.height = 108;
  const wctx = work.getContext('2d', { willReadFrequently: true });

  const streams = new WeakMap();   // source canvas -> {video} for captureStream path
  const state = new Map();         // id -> per-seat live state
  let detector = null;
  let lastCount = -1;
  let running = false;
  let provider = null;
  let lastError = null;
  let initState = 'not started';
  const pathStats = { video: 0, canvas: 0, stream: 0, blank: 0, none: 0 };
  const stats = { checks: 0, windowStart: 0, cps: 0 };

  function blank(ctx) {
    // Sample a coarse grid; a dead source reads uniformly near-black.
    const d = ctx.getImageData(0, 0, work.width, work.height).data;
    let lum = 0, spread = 0, prev = -1;
    for (let i = 0; i < d.length; i += 4 * 97) {
      const l = d[i] + d[i + 1] + d[i + 2];
      lum += l;
      if (prev >= 0) spread += Math.abs(l - prev);
      prev = l;
    }
    return lum < 400 && spread < 200;
  }

  function drawFrom(src) {
    try {
      wctx.drawImage(src, 0, 0, work.width, work.height);
      return !blank(wctx);
    } catch (e) { return false; }
  }

  async function streamVideoFor(canvas) {
    let entry = streams.get(canvas);
    if (entry) return entry.video;
    const video = document.createElement('video');
    video.muted = true; video.playsInline = true;
    video.srcObject = canvas.captureStream(2);
    video.style.cssText = 'position:absolute;left:-9999px;width:96px;height:54px';
    document.body.appendChild(video);
    try { await video.play(); } catch (e) { /* headless autoplay is allowed */ }
    await new Promise((r) => {
      if (video.readyState >= 2) return r();
      video.onloadeddata = r; setTimeout(r, 700);
    });
    streams.set(canvas, { video });
    return video;
  }

  // Get the tile's current pixels onto the work canvas. Returns the path
  // used, or null when every path came up blank.
  async function grab(el) {
    const vid = (el.tagName === 'VIDEO') ? el : el.querySelector && el.querySelector('video');
    if (vid && vid.readyState >= 2 && drawFrom(vid)) { pathStats.video += 1; return 'video'; }
    const canvas = (el.tagName === 'CANVAS') ? el : el.querySelector && el.querySelector('canvas');
    if (canvas) {
      if (drawFrom(canvas)) { pathStats.canvas += 1; return 'canvas'; }
      try {
        const sv = await streamVideoFor(canvas);
        if (sv.readyState >= 2 && drawFrom(sv)) { pathStats.stream += 1; return 'stream'; }
      } catch (e) { /* fall through */ }
      pathStats.blank += 1; return null;
    }
    pathStats.none += 1; return null;
  }

  async function init(locateBase) {
    if (detector) return true;
    if (typeof FaceDetection === 'undefined') {
      initState = 'detector library not loaded';
      return false;
    }
    try {
      detector = new FaceDetection({ locateFile: (f) => locateBase + f });
      detector.setOptions({ model: 'short', minDetectionConfidence: 0.5 });
      detector.onResults((res) => { lastCount = (res.detections || []).length; });
      await detector.initialize();
      initState = 'ready';
      return true;
    } catch (e) {
      detector = null;
      initState = 'detector failed: ' + String(e && e.message || e).slice(0, 160);
      return false;
    }
  }

  async function cycle() {
    const tiles = (provider && provider()) || [];
    for (const t of tiles) {
      if (!running) return;
      let path = null;
      try { path = await grab(t.el); } catch (e) { path = null; }
      const now = Date.now();
      const prev = state.get(String(t.id)) || {};
      if (!path) {
        state.set(String(t.id), { ...prev, lastCheckedAt: now, readable: false });
        continue;
      }
      lastCount = -1;
      try { await detector.send({ image: work }); } catch (e) {
        lastError = String(e && e.message || e).slice(0, 160);
        continue;
      }
      const faces = lastCount < 0 ? 0 : lastCount;
      state.set(String(t.id), {
        readable: true,
        path,
        lastCheckedAt: now,
        facePresent: faces > 0,
        faceCount: faces,
        lastFaceAt: faces > 0 ? now : (prev.lastFaceAt || null),
      });
      stats.checks += 1;
      if (now - stats.windowStart > 5000) {
        stats.cps = Math.round((stats.checks * 1000) / (now - stats.windowStart) * 10) / 10;
        stats.checks = 0; stats.windowStart = now;
      }
    }
  }

  async function loop() {
    stats.windowStart = Date.now();
    while (running) {
      const started = Date.now();
      try { await cycle(); } catch (e) { lastError = String(e && e.message || e).slice(0, 160); }
      // Each seat aims for one check per second: rest whatever the cycle left.
      const rest = Math.max(120, 1000 - (Date.now() - started));
      await new Promise((r) => setTimeout(r, rest));
    }
  }

  window.SeatWatcher = {
    init,
    start(p) {
      provider = p;
      if (running) return;
      running = true;
      loop();
    },
    stop() { running = false; },
    state() {
      const users = {};
      for (const [id, s] of state.entries()) users[id] = s;
      return {
        enabled: !!detector, running, initState,
        checksPerSecond: stats.cps,
        pixelPaths: { ...pathStats },
        seats: state.size,
        users,
        lastError,
      };
    },
  };
})();
