// The camera picture is kept on: run with `node bot/tests/camera_face_keep.mjs`.
// A plain page with a fake Zoom footer and a fake client, as camera_face.js
// was written to allow. Proves: a picture the host switched off comes back,
// a picture switched off for memory stays off, and a room that does not
// allow video is not pressed forever.
import { createRequire } from 'module';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

// require, so a globally installed Playwright is found through NODE_PATH.
const { chromium } = createRequire(import.meta.url)('playwright');
const here = path.dirname(fileURLToPath(import.meta.url));
const script = fs.readFileSync(path.join(here, '../static/camera_face.js'), 'utf8');
const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
const page = await browser.newPage();
await page.setContent('<div id="zoom-root"><button class="send-video-container__btn" aria-label="start my video"></button></div>');
await page.addScriptTag({ content: script });

const result = await page.evaluate(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const btn = document.querySelector('.send-video-container__btn');
  let on = false;
  let hostAllows = true;
  let presses = 0;
  btn.addEventListener('click', () => {
    presses += 1;
    if (!hostAllows) return;
    on = !on;
    btn.setAttribute('aria-label', on ? 'stop my video' : 'start my video');
  });
  const client = { getCurrentUser: () => ({ bVideoOn: on }) };
  const state = { notes: [] };
  const out = {};
  let allow = true;
  await CameraFace.start({ client, state, stepMs: 20, clickGapMs: 50, deadlineMs: 2000 });
  out.onAfterJoin = on;
  const stopKeeper = CameraFace.keep({
    client, state, allowed: () => allow, everyMs: 60, quickTries: 3, slowMs: 100000,
    startOpts: { stepMs: 20, clickGapMs: 50, deadlineMs: 400, maxClicks: 2 },
  });
  await sleep(200);
  out.restartsWhileOn = state.restarts;
  out.liveOnWhileOn = state.liveOn;

  // The host presses Stop Video on the bot.
  on = false; btn.setAttribute('aria-label', 'start my video');
  await sleep(700);
  out.backOn = on;
  out.restartsAfterHostStop = state.restarts;
  out.liveOnAfter = state.liveOn;

  // Switched off to make room: stays off.
  allow = 'switched off to make room, staying off';
  on = false; btn.setAttribute('aria-label', 'start my video');
  const before = presses;
  await sleep(500);
  out.staysOffForMemory = on === false && presses === before;
  out.keeperSays = state.keeper;

  // A room where pressing does nothing: a few tries, then it slows down.
  allow = true; hostAllows = false;
  await sleep(4000);
  const pressesThen = presses;
  const restartsThen = state.restarts;
  await sleep(1500);
  out.slowedDown = presses === pressesThen && state.restarts === restartsThen;
  out.triesBeforeSlowing = restartsThen - out.restartsAfterHostStop;
  out.slowWords = state.keeper;
  stopKeeper();
  return out;
});
await browser.close();
console.log(JSON.stringify(result, null, 2));
const ok = result.onAfterJoin === true && result.restartsWhileOn === 0 && result.liveOnWhileOn === true
  && result.backOn === true && result.restartsAfterHostStop === 1 && result.liveOnAfter === true
  && result.staysOffForMemory === true && result.slowedDown === true && result.triesBeforeSlowing === 3;
console.log(ok ? 'camera picture keeper OK' : 'camera picture keeper FAILED');
process.exit(ok ? 0 : 1);
