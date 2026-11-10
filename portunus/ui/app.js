import { usageFor, CMD_LEFT, KEY_C, KEY_V } from "./hid.js";

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
};

const BUSY = ["pairing", "tunnel", "mounting", "starting"];
const TURN_DEGREES = { portrait: 0, landscapeRight: 90, portraitUpsideDown: 180, landscapeLeft: 270 };
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
    this.abort = new AbortController();
    this.fpsTimer = setInterval(() => {
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
          return { codec, description: bytes, optimizeForLatency: true };
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

  onFrame(frame) {
    this.frames += 1;
    this.pending?.close();
    this.pending = frame;
    if (!this.drawScheduled) {
      this.drawScheduled = true;
      requestAnimationFrame(() => this.draw());
    }
  }

  draw() {
    this.drawScheduled = false;
    const frame = this.pending;
    this.pending = null;
    if (!frame) return;
    const w = frame.displayWidth;
    const h = frame.displayHeight;
    // When iOS renders natively in landscape the buffer is already upright;
    // only a portrait buffer on a turned device needs rotating here.
    const r = w > h ? 0 : this.orientation;
    const sideways = r % 180 !== 0;
    const cw = sideways ? h : w;
    const ch = sideways ? w : h;
    if (this.canvas.width !== cw || this.canvas.height !== ch) {
      this.canvas.width = cw;
      this.canvas.height = ch;
      fitCanvas(); // the frame turned or changed size
    }
    const ctx = this.ctx;
    ctx.setTransform(1, 0, 0, 1, cw / 2, ch / 2);
    ctx.rotate((r * Math.PI) / 180);
    ctx.drawImage(frame, -w / 2, -h / 2, w, h);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    this.frameW = w;
    this.frameH = h;
    this.rotation = r;
    frame.close();
  }
}

const player = new Player(ui.canvas);

// Rounded corners follow the phone's own proportion at any window size.
// The canvas box is sized here, not by CSS max-width/max-height: inside a
// flex container Chromium may clamp one side without the other and squash
// the picture. This keeps the frame's exact aspect ratio at any window size.
const REFLECTION_SHARE = 0.8; // canvas height share when the reflection shows
function fitCanvas() {
  const box = ui.screen.getBoundingClientRect();
  if (!box.width || !box.height) return;
  const maxH = box.height * (settings.reflection ? REFLECTION_SHARE : 1);
  const scale = Math.min(box.width / ui.canvas.width, maxH / ui.canvas.height);
  const w = Math.max(1, Math.floor(ui.canvas.width * scale));
  const h = Math.max(1, Math.floor(ui.canvas.height * scale));
  ui.canvas.style.width = `${w}px`;
  ui.canvas.style.height = `${h}px`;
  ui.canvas.style.setProperty("--radius", `${Math.round(Math.min(w, h) * 0.11)}px`);
}
new ResizeObserver(fitCanvas).observe(ui.screen);

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
  BracketLeft: () => rotate("left"),
  BracketRight: () => rotate("right"),
};

document.addEventListener("keydown", (e) => {
  if (state.phase !== "live" || ui.dialog.open) return;
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

async function rotate(direction) {
  try {
    const res = await fetch("/m/rotate", { method: "POST", headers: headers(true), body: JSON.stringify({ direction }) });
    if (!res.ok) throw new Error();
    const data = await res.json();
    const deg = TURN_DEGREES[data.currentDeviceOrientation];
    if (deg !== undefined) player.orientation = deg;
  } catch {
    toast("iPhone tidak mau diputar sekarang.", true);
  }
}

async function pasteToPhone() {
  try {
    const { chars } = await api("/api/clipboard/to-phone", {});
    await chord(CMD_LEFT, KEY_V);
    toast(`Teks PC ditempel di iPhone (${chars} karakter).`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function copyFromPhone() {
  try {
    await chord(CMD_LEFT, KEY_C); // copy whatever is selected on the phone first
    await sleep(350);
    const { chars } = await api("/api/clipboard/from-phone", {});
    toast(`Clipboard iPhone disalin ke PC (${chars} karakter).`);
  } catch (err) {
    toast(err.message, true);
  }
}

async function saveScreenshot() {
  try {
    const result = await bridge.save_screenshot(ui.canvas.toDataURL("image/png"));
    if (result.ok) toast(`Disimpan sebagai ${result.name}.`);
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
  "rotate-left": () => rotate("left"),
  "rotate-right": () => rotate("right"),
  paste: pasteToPhone,
  copy: copyFromPhone,
  ocr: () => (ui.screen.classList.contains("picking") ? stopPick() : startPick()),
  shot: saveScreenshot,
  settings: openSettings,
  update: () => openSettings(),
  disconnect: async () => { await api("/api/disconnect", {}).catch(() => {}); refresh(); },
};

// Every rail button keeps a readable name when its label is hidden on short
// windows: the tooltip shows the name (and the shortcut, if any).
document.querySelectorAll(".rail button").forEach((button) => {
  const label = button.querySelector("span")?.textContent.trim();
  if (!label) return;
  button.setAttribute("aria-label", label);
  button.title = button.title ? `${label} (${button.title})` : label;
});

document.querySelector(".rail").addEventListener("click", (e) => {
  const button = e.target.closest("button[data-action]");
  if (button && !button.disabled) ACTIONS[button.dataset.action]?.();
});

// ---------------------------------------------------------------- settings
function applySettings() {
  if (settings.theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = settings.theme;
  document.body.classList.toggle("reflect", settings.reflection);
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

function render() {
  const live = state.phase === "live";
  document.body.dataset.phase = state.phase;
  ui.setup.hidden = live;
  ui.live.hidden = !live;
  document.querySelectorAll("[data-needs-live] button, button[data-needs-live]").forEach((b) => { b.disabled = !live; });

  if (live && !player.running) {
    player.start();
    ui.hint.textContent = "Klik layar untuk mengetik";
  } else if (!live && player.running) {
    player.stop();
    stopPick();
    dropHeldKeys();
    touching = false;
    scroll.active = false;
    clearTimeout(scroll.timer);
    ui.hint.textContent = "";
  }

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
    ({ token } = await bridge.session());
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
