import { usageFor, CMD_LEFT, KEY_C, KEY_V } from "./hid.js";
import { hevcSize } from "./hevc.js";

const $ = (sel) => document.querySelector(sel);
const ui = {
  setup: $("#setup"), live: $("#live"), devices: $("#devices"), empty: $("#empty"),
  notice: $("#notice"), progress: $("#progress"), progressNote: $("#progress-note"),
  reveal: $("#reveal"), screen: $("#screen"), canvas: $("#canvas"), marquee: $("#marquee"),
  toast: $("#toast"), statusText: $("#status-text"), hint: $("#hint"), fps: $("#fps"),
  dialog: $("#settings"), audioRow: $("#audio-row"), audioDevice: $("#audio-device"),
  audioToggle: $("#audio-toggle"),
  updateButton: $("#update-button"), updateText: $("#update-text"),
  updateCheck: $("#update-check"), updateInstall: $("#update-install"),
  versionCurrent: $("#version-current"),
  typeChip: $("#type-chip"),
  coach: $("#coach"),
  keys: $("#keys"),
};

const BUSY = ["pairing", "tunnel", "mounting", "starting"];
const MAX_FRAME = 8 * 1024 * 1024;
const MAX_DECODE_QUEUE = 12;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let token = "";
let bridge = null;
let settings = { theme: "system", ctrlAsCmd: true, scrollSpeed: 100, reflection: true };
let state = { phase: "boot", devices: [], features: {} };

// ---------------------------------------------------------------- transport
function headers(json) {
  const h = { "X-Portunus-Token": token };
  if (json) h["Content-Type"] = "application/json";
  return h;
}

async function api(path, body) {
  const init = { method: body === undefined ? "GET" : "POST", headers: headers(body !== undefined), cache: "no-store" };
  if (body !== undefined) init.body = JSON.stringify(body);
  const res = await fetch(path, init);
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error(data.error || `Permintaan gagal (${res.status}).`);
  return data;
}

// Input events must reach the phone in order, so they share one promise chain.
// Pointer moves are coalesced: while one move is in flight only the newest waits.
const input = {
  chain: Promise.resolve(),
  latestMove: null,
  moveQueued: false,
  post(path, payload) {
    return fetch(path, { method: "POST", headers: headers(true), body: JSON.stringify(payload) }).catch(() => {});
  },
  send(path, payload) {
    this.chain = this.chain.then(() => this.post(path, payload));
    return this.chain;
  },
  move(point) {
    this.latestMove = point;
    if (this.moveQueued) return;
    this.moveQueued = true;
    this.chain = this.chain.then(() => {
      this.moveQueued = false;
      const p = this.latestMove;
      this.latestMove = null;
      return p ? this.post("/m/touch", { type: "contact", x: p.x, y: p.y }) : undefined;
    });
  },
};

// ---------------------------------------------------------------- toast
let toastTimer = 0;
function toast(text, bad = false, ms = 2600) {
  ui.toast.textContent = text;
  ui.toast.classList.toggle("bad", bad);
  ui.toast.hidden = false;
  ui.toast.classList.remove("enter");
  void ui.toast.offsetWidth; // restart the entrance animation
  ui.toast.classList.add("enter");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { ui.toast.hidden = true; }, ms);
}

// ---------------------------------------------------------------- video
class Player {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d", { alpha: false, desynchronized: true });
    this.running = false;
    this.orientation = 0;
    this.rotation = 0;
    this.frameW = canvas.width;
    this.frameH = canvas.height;
    this.pending = null;
    this.drawScheduled = false;
    this.frames = 0;
    this.lastPli = 0;
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.orientation = 0; // view rotation starts upright for every session
    this.abort = new AbortController();
    this.fpsTimer = setInterval(() => {
      this.fpsNow = this.frames;
      paintStatus();
      ui.fps.textContent = this.frames ? `${this.frames} fps` : "";
      this.frames = 0;
    }, 1000);
    this.loop();
  }

  stop() {
    this.running = false;
    this.abort?.abort();
    clearInterval(this.fpsTimer);
    ui.fps.textContent = "";
    try { this.decoder?.close(); } catch { /* already closed */ }
    this.decoder = null;
    this.pending?.close();
    this.pending = null;
  }

  async fetchCodec() {
    for (let attempt = 0; this.running; attempt += 1) {
      try {
        const res = await fetch("/m/codec", { headers: headers(false), cache: "no-store", signal: this.abort.signal });
        if (res.ok) {
          const { codec, description } = await res.json();
          const bytes = Uint8Array.from(atob(description), (c) => c.charCodeAt(0));
          const config = { codec, description: bytes, optimizeForLatency: true };
          // Give the decoder the real picture size from the SPS. Without it the
          // WebView2 hardware HEVC path assumes 1280x720 and crops the phone
          // screen to its top 720 rows (seen in the field: display 1280x720,
          // visibleRect 1184x720, coded 1184x2576).
          try {
            const size = hevcSize(bytes);
            Object.assign(config, {
              codedWidth: size.codedWidth,
              codedHeight: size.codedHeight,
              displayAspectWidth: size.width,
              displayAspectHeight: size.height,
            });
            this.streamSize = size;
          } catch {
            this.streamSize = null; // unknown layout: decode without hints, as before
          }
          return config;
        }
      } catch (err) {
        if (err.name === "AbortError") return null;
      }
      await sleep(Math.min(400 * (attempt + 1), 3000));
    }
    return null;
  }

  async loop() {
    while (this.running) {
      const config = await this.fetchCodec();
      if (!config) return;
      let support;
      try { support = await VideoDecoder.isConfigSupported(config); } catch { support = { supported: false }; }
      if (!support.supported) {
        toast("Windows belum bisa memutar video HEVC. Pasang HEVC Video Extensions dari Microsoft Store, lalu buka ulang Portunus.", true, 15000);
        ui.hint.textContent = "Video HEVC tidak didukung";
        return;
      }
      this.config = config;
      try {
        await this.pump();
      } catch (err) {
        if (err.name === "AbortError") return;
      }
      if (this.running) await sleep(700);
    }
  }

  freshDecoder() {
    try { this.decoder?.close(); } catch { /* ignore */ }
    this.decoder = new VideoDecoder({
      output: (frame) => this.onFrame(frame),
      error: () => { this.needsKey = true; this.requestKeyframe(); },
    });
    this.decoder.configure(this.config);
    this.needsKey = true;
  }

  requestKeyframe() {
    const now = performance.now();
    if (now - this.lastPli < 1000) return;
    this.lastPli = now;
    fetch("/m/pli", { method: "POST", headers: headers(true), body: "{}" }).catch(() => {});
  }

  async pump() {
    this.freshDecoder();
    const res = await fetch("/m/stream.bin", { headers: headers(false), cache: "no-store", signal: this.abort.signal });
    if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
    const reader = res.body.getReader();
    // Growable byte buffer: [start, end) holds unread data.
    let buf = new Uint8Array(1 << 20);
    let start = 0;
    let end = 0;
    let timestamp = 0;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      if (end + value.length > buf.length) {
        const live = end - start;
        if (live + value.length <= buf.length) {
          buf.copyWithin(0, start, end);
        } else {
          const next = new Uint8Array((live + value.length) * 2);
          next.set(buf.subarray(start, end));
          buf = next;
        }
        start = 0;
        end = live;
      }
      buf.set(value, end);
      end += value.length;
      // Frame: [u32 big-endian length][u8 type][HEVC access unit]
      while (end - start >= 5) {
        const len = ((buf[start] << 24) >>> 0) + (buf[start + 1] << 16) + (buf[start + 2] << 8) + buf[start + 3];
        if (len > MAX_FRAME) throw new Error("frame too large"); // reconnect resyncs
        if (end - start < 4 + len) break;
        const type = buf[start + 4];
        const data = buf.slice(start + 5, start + 4 + len);
        start += 4 + len;
        const key = type === 0 || type === 2;
        // type 2 = keyframe after an upstream drop: start a clean decoder.
        if (type === 2 || this.decoder.state !== "configured") this.freshDecoder();
        if (this.needsKey && !key) continue;
        // If decoding falls behind, skip to the next keyframe instead of
        // queueing frames without limit (memory) and drifting into lag.
        if (!key && this.decoder.decodeQueueSize > MAX_DECODE_QUEUE) {
          this.needsKey = true;
          this.requestKeyframe();
          continue;
        }
        this.needsKey = false;
        try {
          this.decoder.decode(new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp, data }));
        } catch {
          this.needsKey = true;
          this.requestKeyframe();
        }
        timestamp += 16667;
      }
      if (start === end) { start = 0; end = 0; }
    }
  }

  // Second line of defence: if the decoder still reports a smaller visible
  // area than the SPS says, re-wrap the frame with the right visible rect.
  // Allowed by WebCodecs as long as it lies inside the coded frame.
  fixFrame(frame) {
    const s = this.streamSize;
    if (!s || !(frame.codedWidth >= s.width && frame.codedHeight >= s.height)) return frame;
    const v = frame.visibleRect;
    if (v && v.x === 0 && v.y === 0 && v.width === s.width && v.height === s.height
        && frame.displayWidth === s.width && frame.displayHeight === s.height) return frame;
    try {
      const fixed = new VideoFrame(frame, {
        visibleRect: { x: 0, y: 0, width: s.width, height: s.height },
        displayWidth: s.width,
        displayHeight: s.height,
      });
      frame.close();
      this.corrected = (this.corrected || 0) + 1;
      return fixed;
    } catch {
      return frame;
    }
  }

  onFrame(rawFrame) {
    const frame = this.fixFrame(rawFrame);
    this.frames += 1;
    this.pending?.close();
    this.pending = frame;
    if (!this.drawScheduled) {
      this.drawScheduled = true;
      requestAnimationFrame(() => this.draw());
    }
  }

  // Under fast motion iOS may encode a smaller picture into the TOP-LEFT of
  // the usual buffer and fill the rest with flat gray (128,128,128). Find the
  // picture's right and bottom edges on a small copy of the frame.
  collapseCheck(frame, w, h) {
    const PH = 160;
    const PW = Math.max(8, Math.round((PH * w) / h));
    const probe = this.probe || (this.probe = document.createElement("canvas"));
    const pctx = this.probeCtx || (this.probeCtx = probe.getContext("2d", { willReadFrequently: true }));
    probe.width = PW;
    probe.height = PH;
    try {
      pctx.drawImage(frame, 0, 0, PW, PH);
      const px = pctx.getImageData(0, 0, PW, PH).data;
      const gray = (x, y) => {
        const i = (y * PW + x) * 4;
        return Math.abs(px[i] - 128) < 6 && Math.abs(px[i + 1] - 128) < 6 && Math.abs(px[i + 2] - 128) < 6;
      };
      let right = 0;
      for (let x = 0; x < PW; x += 1) {
        let g = 0; let n = 0;
        for (let y = 0; y < PH; y += 3) { n += 1; if (gray(x, y)) g += 1; }
        if (g / n < 0.6) right = x;
      }
      let bottom = 0;
      for (let y = 0; y < PH; y += 1) {
        let g = 0; let n = 0;
        for (let x = 0; x < PW; x += 3) { n += 1; if (gray(x, y)) g += 1; }
        if (g / n < 0.6) bottom = y;
      }
      const cw = Math.round(((right + 1) / PW) * w);
      const ch = Math.round(((bottom + 1) / PH) * h);
      if (cw > w * 0.2 && cw < w * 0.92 && ch > h * 0.2 && ch < h * 0.92) return [cw, ch];
    } catch { /* readback can fail transiently; draw the full frame */ }
    return null;
  }

  draw() {
    this.drawScheduled = false;
    const frame = this.pending;
    this.pending = null;
    if (!frame) return;
    const w = frame.displayWidth;
    const h = frame.displayHeight;
    // orientation = the view rotation chosen in the rail. iOS keeps sending a
    // portrait buffer even when an app (YouTube full screen) draws sideways,
    // and the device cannot be asked which way it is held, so the user turns
    // the view; touches are mapped back through the same rotation.
    const r = this.orientation;
    const sideways = r % 180 !== 0;
    const cw = sideways ? h : w;
    const ch = sideways ? w : h;
    if (this.canvas.width !== cw || this.canvas.height !== ch) {
      this.canvas.width = cw;
      this.canvas.height = ch;
      fitCanvas(); // the frame turned or changed size
      reportGeometry(frame, this);
    }
    const ctx = this.ctx;
    // Resolution collapse: draw only the real picture, stretched to the full size.
    const crop = this.collapseCheck(frame, w, h);
    ctx.setTransform(1, 0, 0, 1, cw / 2, ch / 2);
    ctx.rotate((r * Math.PI) / 180);
    ctx.drawImage(frame, 0, 0, crop ? crop[0] : w, crop ? crop[1] : h, -w / 2, -h / 2, w, h);
    this.crop = crop;
    diag.update(frame, this);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    this.frameW = w;
    this.frameH = h;
    this.rotation = r;
    frame.close();
  }
}

// Diagnostics (Ctrl+Shift+D): the numbers needed to explain any odd picture
// from one screenshot, without guessing.
let appVersion = "?";
const diag = {
  el: null,
  last: 0,
  toggle() {
    if (!this.el) {
      this.el = document.createElement("pre");
      this.el.className = "diag";
      ui.screen.append(this.el);
    } else {
      this.el.remove();
      this.el = null;
    }
  },
  update(frame, p) {
    if (!this.el || performance.now() - this.last < 250) return;
    this.last = performance.now();
    const c = ui.canvas.getBoundingClientRect();
    const a = ui.screen.getBoundingClientRect();
    const engine = (navigator.userAgent.match(/(Edg|Chrome|Firefox)\/[\d.]+/) || ["?"])[0];
    this.el.textContent = [
      `Portunus ${appVersion}  ${engine}`,
      `frame   ${frame.displayWidth}x${frame.displayHeight}  coded ${frame.codedWidth ?? "-"}x${frame.codedHeight ?? "-"}`,
      `visible ${frame.visibleRect ? `${frame.visibleRect.x},${frame.visibleRect.y} ${frame.visibleRect.width}x${frame.visibleRect.height}` : "-"}  codec ${p.config?.codec || "-"}`,
      `isi     ${p.crop ? `${p.crop[0]}x${p.crop[1]} (collapse diregangkan)` : "penuh"}`,
      `sps     ${p.streamSize ? `${p.streamSize.width}x${p.streamSize.height} (coded ${p.streamSize.codedWidth}x${p.streamSize.codedHeight})` : "-"}  dikoreksi ${p.corrected || 0} frame`,
      `canvas  ${ui.canvas.width}x${ui.canvas.height}  tampil ${Math.round(c.width)}x${Math.round(c.height)}`,
      `area    ${Math.round(a.width)}x${Math.round(a.height)}  dpr ${devicePixelRatio}  ${p.fpsNow ?? 0} fps`,
      `arah    iPhone ${p.orientation}°  rotasi ${p.rotation}°  pantulan ${settings.reflection ? "ya" : "tidak"}`,
    ].join("\n");
  },
};

// Frame geometry goes to portunus.log whenever it changes, so a wrong-looking
// picture can be explained from the log alone.
let lastGeometry = "";
function reportGeometry(frame, p) {
  const v = frame.visibleRect;
  const info = {
    display: [frame.displayWidth, frame.displayHeight],
    coded: [frame.codedWidth ?? null, frame.codedHeight ?? null],
    visible: v ? [v.x, v.y, v.width, v.height] : null,
    canvas: [ui.canvas.width, ui.canvas.height],
    orientation: p.orientation,
    codec: p.config?.codec || null,
    crop: p.crop || null,
    sps: p.streamSize ? [p.streamSize.width, p.streamSize.height] : null,
    corrected: Boolean(p.corrected),
  };
  const key = JSON.stringify(info);
  if (key === lastGeometry) return;
  lastGeometry = key;
  fetch("/api/diag", { method: "POST", headers: headers(true), body: key }).catch(() => {});
}

const player = new Player(ui.canvas);

// Rounded corners follow the phone's own proportion at any window size.
// The canvas box is sized here, not by CSS max-width/max-height: inside a
// flex container Chromium may clamp one side without the other and squash
// the picture. This keeps the frame's exact aspect ratio at any window size.
const REFLECTION_SHARE = 0.8; // canvas height share when the reflection shows
const FRAME = 16; // device frame: 7px bezel + 1px border on each side
const phoneArea = $("#phone-area");
const phoneFrame = $("#phone-frame");
function fitCanvas() {
  const box = phoneArea.getBoundingClientRect();
  if (!box.width || !box.height) return;
  const availW = box.width - FRAME;
  const availH = (box.height - FRAME) * (settings.reflection ? REFLECTION_SHARE : 1);
  const scale = Math.min(availW / ui.canvas.width, availH / ui.canvas.height);
  const w = Math.max(1, Math.floor(ui.canvas.width * scale));
  const h = Math.max(1, Math.floor(ui.canvas.height * scale));
  ui.canvas.style.width = `${w}px`;
  ui.canvas.style.height = `${h}px`;
  phoneFrame.style.setProperty("--radius", `${Math.round(Math.min(w, h) * 0.11)}px`);
}
new ResizeObserver(fitCanvas).observe(phoneArea);

// ---------------------------------------------------------------- touch
function toHid(clientX, clientY) {
  const rect = ui.canvas.getBoundingClientRect();
  const sx = ((clientX - rect.left) * ui.canvas.width) / rect.width;
  const sy = ((clientY - rect.top) * ui.canvas.height) / rect.height;
  const dx = sx - ui.canvas.width / 2;
  const dy = sy - ui.canvas.height / 2;
  const W = player.frameW;
  const H = player.frameH;
  let bx;
  let by;
  switch (player.rotation) { // inverse of the rotation used in draw()
    case 90: bx = dy + W / 2; by = -dx + H / 2; break;
    case 180: bx = -dx + W / 2; by = -dy + H / 2; break;
    case 270: bx = -dy + W / 2; by = dx + H / 2; break;
    default: bx = dx + W / 2; by = dy + H / 2;
  }
  const clamp = (v) => Math.max(0, Math.min(65535, Math.round(v)));
  return { x: clamp((bx / W) * 65535), y: clamp((by / H) * 65535) };
}

let touching = false;
let picking = null; // OCR selection: { x0, y0 } in client coords while dragging

ui.canvas.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  ui.screen.focus({ preventScroll: true });
  ui.canvas.setPointerCapture(e.pointerId);
  if (ui.screen.classList.contains("picking")) {
    picking = { x0: e.clientX, y0: e.clientY };
    updateMarquee(e.clientX, e.clientY);
    return;
  }
  endScroll();
  touching = true;
  ripple(e.clientX, e.clientY);
  input.send("/m/touch", { type: "contact", ...toHid(e.clientX, e.clientY) });
});

ui.canvas.addEventListener("pointermove", (e) => {
  if (picking) { updateMarquee(e.clientX, e.clientY); return; }
  if (touching) input.move(toHid(e.clientX, e.clientY));
});

// A short ring where the finger landed, so a click visibly "touches" the phone.
function ripple(x, y) {
  const host = ui.screen.getBoundingClientRect();
  const dot = document.createElement("span");
  dot.className = "ripple";
  dot.style.left = `${x - host.left}px`;
  dot.style.top = `${y - host.top}px`;
  dot.addEventListener("animationend", () => dot.remove(), { once: true });
  ui.screen.append(dot);
  setTimeout(() => dot.remove(), 800); // reduced-motion has no animationend
}

function liftPointer(e) {
  if (picking) { finishPick(e.clientX, e.clientY); return; }
  if (!touching) return;
  touching = false;
  input.send("/m/touch", { type: "release", ...toHid(e.clientX, e.clientY) });
}
ui.canvas.addEventListener("pointerup", liftPointer);
ui.canvas.addEventListener("pointercancel", liftPointer);
ui.canvas.addEventListener("contextmenu", (e) => e.preventDefault());

// Mouse wheel and touchpad scrolling become a finger drag at the cursor.
const scroll = { active: false, x: 0, y: 0, timer: 0 };
function endScroll() {
  if (!scroll.active) return;
  clearTimeout(scroll.timer);
  scroll.active = false;
  input.send("/m/touch", { type: "release", ...toHid(scroll.x, scroll.y) });
}

ui.canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  if (touching || ui.screen.classList.contains("picking")) return;
  const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1;
  const k = (settings.scrollSpeed / 100) * unit;
  const rect = ui.canvas.getBoundingClientRect();
  if (!scroll.active) {
    scroll.active = true;
    scroll.x = e.clientX;
    scroll.y = e.clientY;
    input.send("/m/touch", { type: "contact", ...toHid(scroll.x, scroll.y) });
  }
  scroll.x -= e.deltaX * k;
  scroll.y -= e.deltaY * k;
  const margin = 12;
  const outside = scroll.x < rect.left + margin || scroll.x > rect.right - margin
    || scroll.y < rect.top + margin || scroll.y > rect.bottom - margin;
  if (outside) {
    scroll.x = Math.max(rect.left + margin, Math.min(rect.right - margin, scroll.x));
    scroll.y = Math.max(rect.top + margin, Math.min(rect.bottom - margin, scroll.y));
    input.move(toHid(scroll.x, scroll.y));
    endScroll();
    return;
  }
  input.move(toHid(scroll.x, scroll.y));
  clearTimeout(scroll.timer);
  scroll.timer = setTimeout(endScroll, 140);
}, { passive: false });

// ---------------------------------------------------------------- keyboard
const held = new Set();
let swallowUntilRelease = false;

function sendKeys() {
  input.send("/m/key", { usages: [...held] });
}

function dropHeldKeys() {
  if (held.size) { held.clear(); sendKeys(); }
}

const SHORTCUTS = {
  KeyH: () => pressButton("home"),
  KeyL: () => pressButton("lock"),
  KeyV: () => pasteToPhone(),
  KeyC: () => copyFromPhone(),
  KeyO: () => startPick(),
  KeyS: () => saveScreenshot(),
  BracketRight: () => rotate("left"),  // same as clicking Putar
  BracketLeft: () => rotate("right"),   // the other way
  KeyD: () => diag.toggle(),
};

document.addEventListener("keydown", (e) => {
  if (ui.dialog.open || ui.keys.open) return;
  const typing = document.activeElement === ui.screen;
  if (e.code === "F11" && state.phase === "live") { // never forwarded to the phone
    e.preventDefault();
    toggleFocus();
    return;
  }
  if (!typing && e.key === "?" && !e.ctrlKey && !e.altKey) {
    e.preventDefault();
    openKeys();
    return;
  }
  if (!typing && e.code === "Escape" && document.body.classList.contains("focus")) {
    e.preventDefault();
    toggleFocus(false);
    return;
  }
  if (state.phase !== "live") return;
  if (e.code === "Escape" && ui.screen.classList.contains("picking")) {
    e.preventDefault();
    stopPick();
    return;
  }
  if (e.ctrlKey && e.shiftKey && SHORTCUTS[e.code]) {
    e.preventDefault();
    dropHeldKeys();
    swallowUntilRelease = true;
    if (!e.repeat) SHORTCUTS[e.code]();
    return;
  }
  if (document.activeElement !== ui.screen || swallowUntilRelease) return;
  const usage = usageFor(e.code, settings.ctrlAsCmd);
  if (usage === undefined) return;
  e.preventDefault(); // keep WebView from acting on Ctrl+P, F5, Tab, and so on
  if (e.repeat || held.has(usage)) return; // iOS repeats held keys itself
  held.add(usage);
  sendKeys();
});

document.addEventListener("keyup", (e) => {
  if (swallowUntilRelease) {
    if (!e.ctrlKey && !e.shiftKey) swallowUntilRelease = false;
    return;
  }
  const usage = usageFor(e.code, settings.ctrlAsCmd);
  if (usage === undefined || !held.has(usage)) return;
  e.preventDefault();
  held.delete(usage);
  sendKeys();
});

ui.screen.addEventListener("focus", () => {
  ui.hint.textContent = "Mengetik ke iPhone. Klik di luar layar untuk berhenti.";
  ui.typeChip.hidden = false;
});
ui.screen.addEventListener("blur", () => {
  dropHeldKeys();
  ui.hint.textContent = "Klik layar untuk mengetik";
  ui.typeChip.hidden = true;
});
window.addEventListener("blur", dropHeldKeys);

async function chord(...usages) {
  input.send("/m/key", { usages });
  await sleep(40);
  return input.send("/m/key", { usages: [] });
}

// ---------------------------------------------------------------- actions
function pressButton(name) {
  input.send("/m/button", { name, state: "press" });
}

function rotate(direction) {
  player.orientation = (player.orientation + (direction === "left" ? 270 : 90)) % 360;
  player.lastPli = 0;
  player.requestKeyframe(); // redraw now, even if the phone screen is still
  flash("rotate", true);
}

async function pasteToPhone() {
  try {
    const { chars } = await api("/api/clipboard/to-phone", {});
    await chord(CMD_LEFT, KEY_V);
    toast(`Teks PC ditempel di iPhone (${chars} karakter).`);
    flash("paste", true);
  } catch (err) {
    toast(err.message, true);
    flash("paste", false);
  }
}

async function copyFromPhone() {
  try {
    await chord(CMD_LEFT, KEY_C); // copy whatever is selected on the phone first
    await sleep(350);
    const { chars } = await api("/api/clipboard/from-phone", {});
    toast(`Clipboard iPhone disalin ke PC (${chars} karakter).`);
    flash("copy", true);
  } catch (err) {
    toast(err.message, true);
    flash("copy", false);
  }
}

async function saveScreenshot() {
  try {
    const result = await bridge.save_screenshot(ui.canvas.toDataURL("image/png"));
    if (result.ok) { toast(`Disimpan sebagai ${result.name}.`); flash("shot", true); }
    else if (!result.cancelled) toast(result.error, true);
  } catch {
    toast("Gambar tidak bisa disimpan.", true);
  }
}

// OCR: drag a box over the screen, or click once to read the whole screen.
function startPick() {
  if (!state.features.ocr) {
    toast("Baca teks butuh paket OCR Windows. Lihat README.", true);
    return;
  }
  endScroll();
  ui.screen.classList.add("picking");
  setPressed("ocr", true);
  toast("Seret kotak di atas teks. Klik sekali untuk seluruh layar. Esc untuk batal.", false, 5000);
}

function stopPick() {
  picking = null;
  ui.marquee.hidden = true;
  ui.screen.classList.remove("picking");
  setPressed("ocr", false);
}

function updateMarquee(x, y) {
  const host = ui.screen.getBoundingClientRect();
  const left = Math.min(picking.x0, x);
  const top = Math.min(picking.y0, y);
  Object.assign(ui.marquee.style, {
    left: `${left - host.left}px`, top: `${top - host.top}px`,
    width: `${Math.abs(x - picking.x0)}px`, height: `${Math.abs(y - picking.y0)}px`,
  });
  ui.marquee.hidden = false;
}

async function finishPick(x, y) {
  const rect = ui.canvas.getBoundingClientRect();
  const sx = ui.canvas.width / rect.width;
  const sy = ui.canvas.height / rect.height;
  let left = Math.max(rect.left, Math.min(picking.x0, x));
  let top = Math.max(rect.top, Math.min(picking.y0, y));
  let right = Math.min(rect.right, Math.max(picking.x0, x));
  let bottom = Math.min(rect.bottom, Math.max(picking.y0, y));
  if (right - left < 8 || bottom - top < 8) {
    left = rect.left; top = rect.top; right = rect.right; bottom = rect.bottom;
  }
  stopPick();
  const cw = Math.round((right - left) * sx);
  const ch = Math.round((bottom - top) * sy);
  // Small regions are enlarged; the Windows engine reads larger glyphs better.
  const scale = ch < 500 ? 2 : 1;
  const crop = document.createElement("canvas");
  crop.width = cw * scale;
  crop.height = ch * scale;
  const ctx = crop.getContext("2d");
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(ui.canvas, Math.round((left - rect.left) * sx), Math.round((top - rect.top) * sy), cw, ch, 0, 0, crop.width, crop.height);
  const png = crop.toDataURL("image/png").split(",", 2)[1];
  try {
    const result = await api("/api/ocr", { png });
    toast(result.chars ? `Teks terbaca dan disalin ke PC (${result.lines} baris).` : "Tidak ada teks yang terbaca di area itu.", !result.chars);
    flash("ocr", Boolean(result.chars));
  } catch (err) {
    toast(err.message, true);
  }
}

function setPressed(action, on) {
  document.querySelector(`[data-action="${action}"]`)?.setAttribute("aria-pressed", String(on));
}

const ACTIONS = {
  home: () => pressButton("home"),
  lock: () => pressButton("lock"),
  "volume-up": () => pressButton("volume-up"),
  "volume-down": () => pressButton("volume-down"),
  // Default turn is counter-clockwise: it uprights YouTube's full-screen
  // picture, which iOS draws clockwise-sideways in the portrait buffer.
  rotate: (e) => rotate(e?.shiftKey ? "right" : "left"),
  focus: () => toggleFocus(),
  more: () => toggleMore(),
  keys: () => openKeys(),
  reflect: async () => {
    try { settings = await api("/api/settings", { reflection: !settings.reflection }); } catch { settings.reflection = !settings.reflection; }
    applySettings();
  },
  paste: pasteToPhone,
  copy: copyFromPhone,
  ocr: () => (ui.screen.classList.contains("picking") ? stopPick() : startPick()),
  shot: saveScreenshot,
  settings: openSettings,
  update: () => openSettings(),
  disconnect: async () => { await api("/api/disconnect", {}).catch(() => {}); refresh(); },
  "focus-screen": () => { if (state.phase === "live") ui.screen.focus({ preventScroll: true }); },
  "rail-toggle": async () => {
    try { settings = await api("/api/settings", { railCompact: !settings.railCompact }); } catch { settings.railCompact = !settings.railCompact; }
    applySettings();
    hideTip();
  },
};

// ---------------------------------------------------------------- rail
const rail = $("#rail");
const indicator = $("#rail-indicator");
const tip = $("#tip");
const railButtons = () => [...rail.querySelectorAll("button")].filter((b) => !b.hidden);
let tipTimer = 0;
let hovered = null; // the rail button under the pointer or keyboard focus

// Every button keeps a readable name when labels are hidden; the shortcut
// shows in the tooltip instead of a native title (titles never appear on
// unavailable buttons, which is exactly when an explanation is needed).
function labelOf(button) {
  return button.dataset.tip
    || button.querySelector(":scope > span:not(.head-phone):not(.head-text)")?.textContent.trim()
    || button.getAttribute("aria-label") || "";
}
railButtons().forEach((b) => { if (!b.hasAttribute("aria-label")) b.setAttribute("aria-label", labelOf(b)); });

function unavailable(button) {
  return button.getAttribute("aria-disabled") === "true";
}

function moveIndicator(button) {
  if (!button) { indicator.classList.remove("on"); return; }
  const r = rail.getBoundingClientRect();
  const b = button.getBoundingClientRect();
  indicator.style.width = `${b.width}px`;
  indicator.style.height = `${b.height}px`;
  indicator.style.transform = `translate(${b.left - r.left + rail.scrollLeft}px, ${b.top - r.top + rail.scrollTop}px)`;
  indicator.classList.toggle("muted", unavailable(button));
  indicator.classList.add("on");
}

function showTip(button) {
  if (!button || matchMedia("(max-width: 640px)").matches) return; // no tooltips on the narrow layout
  const keys = (button.dataset.keys || "").split(" ").filter(Boolean);
  tip.querySelector(".tip-label").textContent = button.dataset.action === "focus-screen"
    ? button.getAttribute("aria-label") : labelOf(button);
  tip.querySelector(".tip-keys").replaceChildren(...keys.map((k) => Object.assign(document.createElement("kbd"), { textContent: k })));
  tip.querySelector(".tip-note").textContent = unavailable(button)
    ? (button.dataset.action === "focus-screen" ? "" : "Sambungkan iPhone dulu") : "";
  const b = button.getBoundingClientRect();
  const place = button.closest("#topbar") ? "below" : button.closest("#dock") ? "above" : "right";
  tip.classList.toggle("below", place === "below");
  tip.classList.toggle("above", place === "above");
  if (place === "right") {
    tip.style.left = `${Math.round(b.right + 10)}px`;
    tip.style.top = `${Math.round(b.top + b.height / 2)}px`;
  } else {
    tip.style.top = `${Math.round(place === "below" ? b.bottom + 8 : b.top - 8)}px`;
    tip.style.left = `${Math.round(b.left + b.width / 2)}px`;
  }
  tip.hidden = false;
  if (place !== "right") { // keep inside the window
    const t = tip.getBoundingClientRect();
    if (t.right > innerWidth - 6) tip.style.left = `${Math.round(innerWidth - 6 - t.width / 2)}px`;
  }
}

function hideTip() {
  clearTimeout(tipTimer);
  tip.hidden = true;
}

function hoverButton(button, instant) {
  hovered = button;
  moveIndicator(button);
  clearTimeout(tipTimer);
  if (!tip.hidden || instant) showTip(button); // moving along the rail: no delay
  else tipTimer = setTimeout(() => showTip(button), 320);
}

rail.addEventListener("pointerover", (e) => {
  if (e.pointerType === "touch") return;
  const button = e.target.closest("button");
  if (button && rail.contains(button)) hoverButton(button, false);
});
rail.addEventListener("pointerleave", () => { hovered = null; moveIndicator(null); hideTip(); });

// Header and dock: same actions and tooltips as the rail, no sliding highlight.
for (const bar of [$("#topbar"), $("#dock")]) {
  bar.addEventListener("pointerover", (e) => {
    const button = e.target.closest("button[data-tip]");
    if (!button || e.pointerType === "touch") return;
    clearTimeout(tipTimer);
    if (!tip.hidden) showTip(button);
    else tipTimer = setTimeout(() => showTip(button), 320);
  });
  bar.addEventListener("pointerleave", hideTip);
  bar.addEventListener("click", (e) => {
    const button = e.target.closest("button[data-action]");
    if (!button || unavailable(button)) return;
    hideTip();
    ACTIONS[button.dataset.action]?.(e);
  });
}

// The rail changes size (collapse, window resize): put the highlight back on
// the button it belongs to instead of leaving it where that button used to be.
function realignIndicator() {
  requestAnimationFrame(() => {
    indicator.classList.add("snap"); // jump, do not glide across the changed layout
    moveIndicator(hovered && rail.contains(hovered) && !hovered.hidden ? hovered : null);
    requestAnimationFrame(() => requestAnimationFrame(() => indicator.classList.remove("snap")));
  });
}
new ResizeObserver(realignIndicator).observe(rail);
rail.addEventListener("focusin", (e) => {
  const button = e.target.closest("button");
  if (button && e.target.matches(":focus-visible")) hoverButton(button, true);
});
rail.addEventListener("focusout", () => {
  hideTip();
  if (!rail.matches(":hover")) { hovered = null; moveIndicator(null); }
});
rail.addEventListener("scroll", () => { hideTip(); indicator.classList.remove("on"); }, { passive: true });
window.addEventListener("resize", hideTip);

// Volume repeats while held, like the side buttons on the phone.
let repeatTimer = 0;
function stopRepeat() { clearTimeout(repeatTimer); clearInterval(repeatTimer); repeatTimer = 0; }
rail.addEventListener("pointerdown", (e) => {
  const button = e.target.closest("button[data-repeat]");
  if (!button || unavailable(button) || e.button !== 0) return;
  ACTIONS[button.dataset.action]?.();
  stopRepeat();
  repeatTimer = setTimeout(() => {
    repeatTimer = setInterval(() => ACTIONS[button.dataset.action]?.(), 160);
  }, 420);
});
["pointerup", "pointercancel", "pointerleave"].forEach((type) => rail.addEventListener(type, stopRepeat));

rail.addEventListener("click", (e) => {
  const button = e.target.closest("button[data-action]");
  if (!button || unavailable(button)) return;
  if (button.hasAttribute("data-repeat") && e.detail > 0) return; // handled on pointerdown
  ACTIONS[button.dataset.action]?.(e);
});

// Right-click on Putar turns the other way (wherever the button lives).
document.addEventListener("contextmenu", (e) => {
  const button = e.target.closest('button[data-action="rotate"]');
  if (!button) return;
  e.preventDefault();
  if (!unavailable(button)) rotate("right");
});

// A finished action briefly turns its icon into a check (or a cross).
function flash(action, ok) {
  const button = document.querySelector(`#rail [data-action="${action}"], #dock [data-action="${action}"]`);
  const use = button?.querySelector("use");
  if (!use) return;
  if (!button.dataset.icon) button.dataset.icon = use.getAttribute("href");
  clearTimeout(button._flash);
  button.classList.remove("done", "fail");
  void button.offsetWidth;
  button.classList.add(ok ? "done" : "fail");
  use.setAttribute("href", ok ? "#i-check" : "#i-cross");
  button._flash = setTimeout(() => {
    button.classList.remove("done", "fail");
    use.setAttribute("href", button.dataset.icon);
  }, 950);
}

function paintRail(live) {
  rail.querySelectorAll("[data-needs-live] button, button[data-needs-live]").forEach((b, i) => b.style.setProperty("--i", i));
  rail.querySelectorAll("[data-needs-live] button, button[data-needs-live]").forEach((b) => {
    b.setAttribute("aria-disabled", String(!live));
  });
  const head = $("#rail-head");
  const busy = BUSY.includes(state.phase);
  const via = state.device?.transport === "wifi" ? "Wi-Fi" : "USB";
  $("#rail-transport").textContent = live ? via : busy ? "..." : state.phase === "error" ? "Gagal" : "Offline";
  head.setAttribute("aria-disabled", String(!live));
  head.setAttribute("aria-label", live
    ? `${state.device?.name || "iPhone"} lewat ${via}. Klik untuk mengetik ke iPhone`
    : busy ? "Menghubungkan" : "Belum tersambung");
  const toggle = $("#rail-toggle");
  const compact = Boolean(settings.railCompact);
  toggle.setAttribute("aria-label", compact ? "Lebarkan sidebar" : "Ciutkan sidebar");
  toggle.dataset.tip = compact ? "Lebarkan sidebar" : "Ciutkan sidebar";
  toggle.querySelector("span").textContent = compact ? "Lebarkan" : "Ciutkan";
}

// ---------------------------------------------------------------- settings
function applySettings() {
  document.documentElement.dataset.theme = settings.theme;
  document.body.classList.toggle("reflect", settings.reflection);
  document.querySelector('#dock [data-action="reflect"]')?.setAttribute("aria-pressed", String(Boolean(settings.reflection)));
  document.body.classList.toggle("rail-compact", Boolean(settings.railCompact));
  if (typeof paintRail === "function" && rail) paintRail(state.phase === "live");
  fitCanvas();
}

function openSettings() {
  const form = ui.dialog.querySelector("form");
  form.theme.value = settings.theme;
  form.ctrlAsCmd.checked = settings.ctrlAsCmd;
  form.scrollSpeed.value = settings.scrollSpeed;
  form.reflection.checked = settings.reflection;
  form.autoUpdateCheck.checked = settings.autoUpdateCheck;
  form.allowWifi.checked = settings.allowWifi;
  showUpdate();
  ui.audioRow.hidden = !state.features.audio;
  if (state.features.audio) loadAudioDevices();
  ui.dialog.showModal();
}

ui.dialog.querySelector("[data-close]").addEventListener("click", () => ui.dialog.close());
ui.dialog.querySelector("form").addEventListener("submit", (e) => e.preventDefault());

ui.dialog.querySelector("form").addEventListener("change", async (e) => {
  const field = e.target;
  if (!field.name) return;
  const value = field.type === "checkbox" ? field.checked : field.type === "range" ? Number(field.value) : field.value;
  try {
    settings = await api("/api/settings", { [field.name]: value });
    applySettings();
  } catch (err) {
    toast(err.message, true);
  }
});

async function loadAudioDevices() {
  ui.audioDevice.replaceChildren();
  try {
    const { devices, active } = await api("/api/audio/devices");
    if (!devices.length) {
      ui.audioDevice.append(new Option("Tidak ada perangkat Bluetooth yang cocok", ""));
    }
    for (const d of devices) ui.audioDevice.append(new Option(d.name, d.id, false, d.id === active));
    ui.audioToggle.textContent = active ? "Hentikan" : "Putar di PC";
  } catch (err) {
    ui.audioDevice.append(new Option(err.message, ""));
  }
}

ui.audioToggle.addEventListener("click", async () => {
  const active = state.audioDevice;
  const device = active ? null : ui.audioDevice.value || null;
  if (!active && !device) return;
  ui.audioToggle.disabled = true;
  try {
    const res = await api("/api/audio", { device });
    state.audioDevice = res.active;
    ui.audioToggle.textContent = res.active ? "Hentikan" : "Putar di PC";
    toast(res.active ? "Suara iPhone diputar di PC." : "Suara kembali ke iPhone.");
  } catch (err) {
    toast(err.message, true);
  } finally {
    ui.audioToggle.disabled = false;
  }
});

// ---------------------------------------------------------------- updates
let update = { current: "", supported: false, reason: "", available: null };

function showUpdate(note) {
  ui.versionCurrent.textContent = update.current;
  ui.updateButton.hidden = !update.available;
  ui.updateInstall.hidden = !update.available;
  ui.updateCheck.disabled = !update.supported;
  ui.updateText.textContent = note
    || (!update.supported ? update.reason
      : update.available ? `Versi ${update.available} tersedia.`
      : "Belum diperiksa.");
}

async function checkUpdate(quiet) {
  ui.updateCheck.disabled = true;
  try {
    update = await api("/api/update/check", {});
    showUpdate(update.available ? "" : "Portunus sudah versi terbaru.");
    if (update.available && quiet) toast(`Portunus ${update.available} tersedia. Tekan Perbarui di kiri bawah.`, false, 6000);
  } catch (err) {
    if (!quiet) showUpdate(err.message);
  } finally {
    ui.updateCheck.disabled = !update.supported;
  }
}

async function installUpdate() {
  if (!update.available) return;
  if (!window.confirm(`Pasang Portunus ${update.available}? Sambungan ke iPhone akan diputus dan Portunus dibuka ulang.`)) return;
  ui.updateInstall.disabled = true;
  showUpdate("Mengunduh dan memeriksa tanda tangan.");
  try {
    await api("/api/update/install", {});
    showUpdate("Terverifikasi. Portunus akan dibuka ulang.");
  } catch (err) {
    showUpdate(err.message);
    ui.updateInstall.disabled = false;
  }
}

ui.updateCheck.addEventListener("click", () => checkUpdate(false));
ui.updateInstall.addEventListener("click", installUpdate);

// ---------------------------------------------------------------- state view
let lastDevices = "";

function deviceButton(label, cls, onClick, disabled) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = cls;
  button.textContent = label;
  button.disabled = disabled;
  button.addEventListener("click", onClick);
  return button;
}

function paintDeviceList() {
  const key = JSON.stringify([state.devices, state.phase, settings.allowWifi]);
  if (key === lastDevices) return;
  lastDevices = key;
  const busy = BUSY.includes(state.phase);
  ui.devices.replaceChildren(...state.devices.map((d) => {
    const li = document.createElement("li");
    const text = document.createElement("div");
    text.className = "grow";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = d.name || "iPhone";
    const badge = document.createElement("span");
    badge.className = `badge ${d.transport}`;
    badge.textContent = d.transport === "wifi" ? "Wi-Fi" : "USB";
    name.append(" ", badge);
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${d.ios ? `iOS ${d.ios}, ` : ""}ID ${d.udid.slice(-8)}`;
    text.append(name, meta);
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const connecting = busy && state.udid === d.udid;
    actions.append(deviceButton(connecting ? "Menghubungkan" : "Hubungkan", "primary",
      () => connect(d.udid, d.transport), busy));
    if (settings.allowWifi && d.transport === "usb" && !d.wifiReady) {
      actions.append(deviceButton("Aktifkan Wi-Fi", "ghost", () => wifiAction("enable", d.udid), busy));
    }
    if (d.wifiReady) {
      actions.append(deviceButton("Cabut Wi-Fi", "ghost", () => wifiAction("revoke", d.udid), busy));
    }
    li.append(text, actions);
    return li;
  }));
  ui.empty.hidden = state.devices.length > 0;
  ui.reveal.disabled = busy || !state.devices.some((d) => d.transport === "usb");
}

async function wifiAction(kind, udid) {
  if (kind === "revoke" && !window.confirm("Cabut izin Wi-Fi untuk iPhone ini? PC ini tidak bisa lagi menyambung tanpa kabel sampai diaktifkan ulang.")) return;
  try {
    await api(`/api/wifi/${kind}`, { udid });
    toast(kind === "enable" ? "Wi-Fi aktif. Lain kali iPhone bisa disambungkan tanpa kabel." : "Izin Wi-Fi dicabut.");
  } catch (err) {
    toast(err.message, true);
  }
  lastDevices = "";
  refresh();
}

function renderNotice() {
  ui.notice.replaceChildren();
  let message = "";
  if (!state.usbmuxOk) {
    message = "Layanan Apple Mobile Device tidak ditemukan. Pasang Apple Devices dari Microsoft Store, lalu buka ulang Portunus.";
  } else if (state.phase === "error") {
    message = state.message;
  }
  ui.notice.hidden = !message;
  if (!message) return;
  const p = document.createElement("div");
  p.textContent = message;
  ui.notice.append(p);
  if (state.phase === "error" && state.udid && state.devices.some((d) => d.udid === state.udid)) {
    const retry = document.createElement("button");
    retry.type = "button";
    retry.textContent = "Coba lagi";
    retry.addEventListener("click", () => connect(state.udid));
    ui.notice.append(retry);
  }
}

function renderProgress() {
  const index = BUSY.indexOf(state.phase);
  ui.progress.hidden = index < 0;
  ui.progressNote.textContent = index < 0 ? "" : state.message;
  ui.progress.querySelectorAll("li").forEach((li, i) => {
    li.classList.toggle("done", i < index);
    li.classList.toggle("now", i === index);
  });
}

// Header pill and sidebar info panel: existing state, presented.
function paintStatus() {
  const live = state.phase === "live";
  const busy = BUSY.includes(state.phase);
  const wifi = state.device?.transport === "wifi";
  const name = live ? (state.device?.name || "iPhone") : "";
  $("#conn-text").textContent = live ? `Tersambung lewat ${wifi ? "Wi-Fi" : "USB"}` : busy ? "Menghubungkan"
    : state.phase === "error" ? "Gagal tersambung" : "Belum tersambung";
  $("#conn-device").textContent = name;
  const via = $("#conn-via");
  via.toggleAttribute("hidden", !live); // SVG elements have no .hidden property
  via.querySelector("use").setAttribute("href", wifi ? "#i-wifi" : "#i-usb");
  const status = $("#info-status");
  status.lastChild.textContent = live ? (wifi ? "Wi-Fi" : "USB") : busy ? "Menghubungkan" : state.phase === "error" ? "Gagal" : "Offline";
  $("#info-device").textContent = name || "-";
  const s = player.streamSize;
  $("#info-res").textContent = live ? (s ? `${s.width} x ${s.height}` : `${player.frameW} x ${player.frameH}`) : "-";
  $("#info-fps").textContent = live ? String(player.fpsNow ?? 0) : "-";
}

function render() {
  const live = state.phase === "live";
  document.body.dataset.phase = state.phase;
  ui.setup.hidden = live;
  ui.live.hidden = !live;
  paintRail(live);

  if (live && !player.running) {
    player.start();
    showCoach();
    ui.hint.textContent = "Klik layar untuk mengetik";
  } else if (!live && player.running) {
    player.stop();
    toggleFocus(false);
    closeMore(false);
    stopPick();
    dropHeldKeys();
    touching = false;
    scroll.active = false;
    clearTimeout(scroll.timer);
    ui.hint.textContent = "";
  }

  paintStatus();
  const device = state.device?.name || "iPhone";
  const via = state.device?.transport === "wifi" ? "Wi-Fi" : "USB";
  ui.statusText.textContent = live ? `${device} tersambung lewat ${via}`
    : BUSY.includes(state.phase) ? "Menghubungkan"
    : state.phase === "error" ? "Sambungan gagal"
    : "Belum tersambung";

  paintDeviceList();
  renderNotice();
  renderProgress();
}

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await api("/api/state");
  } catch {
    state = { ...state, phase: "error", message: "Portunus kehilangan kontak dengan prosesnya sendiri. Tutup dan buka ulang." };
  } finally {
    refreshing = false;
  }
  render();
}

async function connect(udid, transport = "usb") {
  try {
    await api("/api/connect", { udid, transport });
  } catch (err) {
    toast(err.message, true);
  }
  refresh();
}

ui.reveal.addEventListener("click", async () => {
  const picked = state.devices.find((d) => d.udid === state.udid);
  const udid = (picked || state.devices[0])?.udid;
  if (!udid) return;
  ui.reveal.disabled = true;
  try {
    await api("/api/reveal-devmode", { udid });
    toast("Menu Developer Mode sudah muncul di Settings iPhone.");
  } catch (err) {
    toast(err.message, true);
  } finally {
    ui.reveal.disabled = false;
  }
});

// ---------------------------------------------------------------- "more" menu
// On narrow windows the bar keeps only the essentials; the rest is listed here.
// Items are built from the rail's own buttons, so labels, icons, shortcuts and
// actions have one source.
const MORE_ITEMS = ["paste", "copy", "ocr", "shot", "|", "disconnect"];
const moreMenu = $("#more-menu");
const moreButton = $("#more-button");

function closeMore(returnFocus) {
  if (moreMenu.hidden) return;
  moreMenu.hidden = true;
  moreButton.setAttribute("aria-expanded", "false");
  if (returnFocus) moreButton.focus();
}

function toggleMore() {
  if (!moreMenu.hidden) { closeMore(true); return; }
  const items = MORE_ITEMS.map((action) => {
    if (action === "|") return document.createElement("hr");
    const source = rail.querySelector(`[data-action="${action}"]`);
    const item = document.createElement("button");
    item.type = "button";
    item.setAttribute("role", "menuitem");
    item.dataset.run = action;
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", source?.dataset.icon || source?.querySelector("use")?.getAttribute("href") || "");
    icon.append(use);
    const label = document.createElement("span");
    label.textContent = labelOf(source);
    item.append(icon, label);
    const keys = source?.dataset.keys;
    if (keys) item.append(Object.assign(document.createElement("kbd"), { textContent: keys.replace(/ /g, "+") }));
    return item;
  });
  moreMenu.replaceChildren(...items);
  moreMenu.hidden = false;
  moreButton.setAttribute("aria-expanded", "true");
  const b = moreButton.getBoundingClientRect();
  const width = moreMenu.offsetWidth;
  moreMenu.style.top = `${Math.round(b.bottom + 6)}px`;
  moreMenu.style.left = `${Math.round(Math.max(8, Math.min(b.right - width, innerWidth - width - 8)))}px`;
  moreMenu.querySelector("button")?.focus();
}

moreMenu.addEventListener("click", (e) => {
  const item = e.target.closest("button[data-run]");
  if (!item) return;
  closeMore(false);
  ACTIONS[item.dataset.run]?.(e);
});
moreMenu.addEventListener("keydown", (e) => {
  const items = [...moreMenu.querySelectorAll("button")];
  const i = items.indexOf(document.activeElement);
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus();
  } else if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    closeMore(true);
  }
});
document.addEventListener("pointerdown", (e) => {
  if (!moreMenu.hidden && !moreMenu.contains(e.target) && !moreButton.contains(e.target)) closeMore(false);
});
window.addEventListener("resize", () => closeMore(false));

// ---------------------------------------------------------------- focus mode
// Full screen with only the phone. The rail becomes a drawer that slides in
// when the pointer touches the left edge; F11 or Esc leaves.
function toggleFocus(force) {
  const on = force ?? !document.body.classList.contains("focus");
  if (on === document.body.classList.contains("focus")) return;
  document.body.classList.toggle("focus", on);
  document.querySelector('#dock [data-action="focus"]')?.setAttribute("aria-pressed", String(on));
  document.body.classList.remove("rail-peek");
  bridge?.set_fullscreen?.(on);
  if (on) toast("Mode fokus. Arahkan kursor ke tepi kiri untuk kontrol, F11 untuk keluar.", false, 3200);
}

let peekTimer = 0;
document.addEventListener("pointermove", (e) => {
  if (!document.body.classList.contains("focus")) return;
  if (e.clientX <= 12) {
    clearTimeout(peekTimer);
    document.body.classList.add("rail-peek");
  }
});
rail.addEventListener("pointerenter", () => clearTimeout(peekTimer));
rail.addEventListener("pointerleave", () => {
  if (!document.body.classList.contains("focus")) return;
  peekTimer = setTimeout(() => document.body.classList.remove("rail-peek"), 350);
});

// ---------------------------------------------------------------- coach & keys
let coachShown = false;
function showCoach() {
  if (coachShown) return;
  coachShown = true;
  ui.coach.hidden = false;
  setTimeout(() => ui.coach.classList.add("leaving"), 5200);
  setTimeout(() => { ui.coach.hidden = true; ui.coach.classList.remove("leaving"); }, 5700);
}

function openKeys() {
  const rows = [...rail.querySelectorAll("button[data-keys]")]
    .filter((b) => b.dataset.keys)
    .map((b) => [b.dataset.keys, labelOf(b)]);
  rows.push(["Ctrl Shift [", "Putar tampilan ke arah sebaliknya"], ["Ctrl Shift D", "Panel diagnostik"],
    ["Esc", "Keluar mode fokus / batal pilih area"], ["?", "Lembar ini"]);
  const list = $("#keys-list");
  list.replaceChildren(...rows.flatMap(([keys, label]) => {
    const dt = document.createElement("dt");
    dt.append(...keys.split(" ").map((k) => Object.assign(document.createElement("kbd"), { textContent: k })));
    const dd = document.createElement("dd");
    dd.textContent = label;
    return [dt, dd];
  }));
  if (ui.dialog.open) ui.dialog.close();
  ui.keys.showModal();
}
ui.keys.querySelector("[data-close-keys]").addEventListener("click", () => ui.keys.close());
$("#open-keys").addEventListener("click", openKeys);

// ---------------------------------------------------------------- boot
function waitForBridge(timeout = 10000) {
  return new Promise((resolve, reject) => {
    const started = performance.now();
    const check = () => {
      if (window.pywebview?.api?.session) { resolve(window.pywebview.api); return; }
      if (performance.now() - started > timeout) { reject(new Error("no bridge")); return; }
      setTimeout(check, 50);
    };
    window.addEventListener("pywebviewready", check, { once: true });
    check();
  });
}

async function boot() {
  try {
    bridge = await waitForBridge();
    ({ token, version: appVersion } = await bridge.session());
    if (!token) throw new Error("refused");
  } catch {
    ui.statusText.textContent = "Halaman ini hanya berjalan di dalam aplikasi Portunus.";
    return;
  }
  try { settings = await api("/api/settings"); } catch { /* keep defaults */ }
  applySettings();
  await refresh();
  setInterval(refresh, 1000);
  try { update = await api("/api/update"); } catch { /* keep defaults */ }
  showUpdate();
  if (update.supported && settings.autoUpdateCheck) checkUpdate(true);
}

boot();
