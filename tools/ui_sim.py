"""Render the real Portunus UI in headless Firefox with a mock backend.

    python tools/ui_sim.py test            # repeated layout tests, many screens and windows
    python tools/ui_sim.py video out.mp4   # scripted walkthrough recorded as a video
    python tools/ui_sim.py video-rail out.mp4  # sidebar interactions only

Developer tool only (not shipped in the app). Needs: selenium, Firefox with
geckodriver (FIREFOX / GECKODRIVER env vars), and ffmpeg for the video.

The UI files are copied to a temp folder; a stub for the pywebview bridge and
simulation hooks are added to the COPY only. Frames are synthetic phone
screens drawn on a canvas at each device's real pixel size.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "portunus" / "ui"

DEVICES = [  # name, width, height (portrait pixels)
    ("iPhone SE", 750, 1334),
    ("iPhone 13 mini", 1080, 2340),
    ("iPhone 15", 1179, 2556),
    ("iPhone 15 Pro Max", 1290, 2796),
    ("iPad Air", 1640, 2360),
    ("Pixel 8 (rasio Android)", 1080, 2400),
    ("Galaxy S24 Ultra (rasio Android)", 1440, 3120),
    ("Galaxy Z Fold (rasio Android)", 1812, 2176),
]
WINDOWS = [(1120, 900), (800, 700), (560, 640), (1920, 1080), (1400, 600), (700, 1000)]

STUB_JS = """
window.pywebview = { api: {
  session: async () => ({ token: "sim", version: "sim" }),
  save_screenshot: async () => ({ ok: true, name: "portunus.png" }),
} };
window.__caption = (text) => {
  let el = document.getElementById("sim-caption");
  if (!el) {
    el = document.createElement("div");
    el.id = "sim-caption";
    el.style.cssText = "position:fixed;left:50%;bottom:44px;transform:translateX(-50%);z-index:99;" +
      "padding:8px 16px;border-radius:10px;background:rgba(10,14,16,.82);color:#fff;" +
      "font:600 15px system-ui,sans-serif;pointer-events:none;max-width:90%;text-align:center";
    document.body.append(el);
  }
  el.textContent = text;
  el.hidden = !text;
};
"""

HOOKS_JS = """
// ---- simulation hooks (appended to a temporary copy by tools/ui_sim.py) ----
const SIM_COLORS = ["#ff6b5e", "#ffb340", "#36c96b", "#2fa3ff", "#8d6bff", "#ff5fa2", "#22c3c3", "#f2c94c"];
function simPaint(c, d, t) {
  const g = c.getContext("2d");
  const W = c.width, H = c.height, land = W > H, u = Math.min(W, H) / 100;
  const grad = g.createLinearGradient(0, 0, W, H);
  grad.addColorStop(0, d.android ? "#1f3b2c" : "#20324f");
  grad.addColorStop(1, d.android ? "#6fa06b" : "#c06a8a");
  g.fillStyle = grad; g.fillRect(0, 0, W, H);
  g.fillStyle = "#fff"; g.font = `600 ${4.2 * u}px system-ui`; g.textBaseline = "middle";
  const now = new Date(); g.fillText(`${now.getHours()}:${String(now.getMinutes()).padStart(2, "0")}`, 7 * u, 5 * u);
  g.fillRect(W - 15 * u, 3.3 * u, 9 * u, 3.6 * u);
  const cols = land ? 8 : 4, size = (land ? W : W) / (cols * 1.6), gap = size * 0.6;
  const rows = land ? 2 : 5, x0 = (W - (cols * size + (cols - 1) * gap)) / 2, y0 = land ? 14 * u : 18 * u;
  for (let r = 0; r < rows; r++) for (let k = 0; k < cols; k++) {
    g.fillStyle = SIM_COLORS[(r * cols + k) % SIM_COLORS.length];
    g.beginPath(); g.roundRect(x0 + k * (size + gap), y0 + r * (size + gap * 1.2), size, size, size * 0.24); g.fill();
  }
  const pulse = 0.5 + 0.5 * Math.sin(t / 500);
  g.fillStyle = `rgba(255,255,255,${0.18 + 0.12 * pulse})`;
  g.beginPath(); g.roundRect(W * 0.06, H - (land ? 22 : 13) * u, W * 0.88, (land ? 16 : 10) * u, 4 * u); g.fill();
  g.fillStyle = "rgba(0,0,0,.55)";
  g.beginPath(); g.roundRect(W * 0.12, H * 0.5 - 7 * u, W * 0.76, 14 * u, 3 * u); g.fill();
  g.fillStyle = "#fff"; g.textAlign = "center"; g.font = `700 ${4.4 * u}px system-ui`;
  g.fillText(d.label, W / 2, H * 0.5 - 2.4 * u);
  g.font = `500 ${3.2 * u}px system-ui`;
  g.fillText(`${W} x ${H} px`, W / 2, H * 0.5 + 3.4 * u);
  g.textAlign = "left";
}
Player.prototype.loop = async function () {
  const frame = document.createElement("canvas");
  while (this.running) {
    const d = window.__device || { w: 1179, h: 2556, label: "iPhone 15" };
    if (frame.width !== d.w || frame.height !== d.h) { frame.width = d.w; frame.height = d.h; }
    simPaint(frame, d, performance.now());
    if (d.collapse) { // mimic iOS resolution collapse: small picture top-left, gray padding
      const tmp = document.createElement("canvas"); tmp.width = frame.width; tmp.height = frame.height;
      tmp.getContext("2d").drawImage(frame, 0, 0);
      const g = frame.getContext("2d");
      g.fillStyle = "rgb(128,128,128)"; g.fillRect(0, 0, frame.width, frame.height);
      g.drawImage(tmp, 0, 0, Math.round(frame.width * 0.6), Math.round(frame.height * 0.6));
    }
    frame.displayWidth = d.w; frame.displayHeight = d.h; frame.close = () => {};
    this.onFrame(frame);
    await sleep(40);
  }
};
window.__ui = { fitCanvas, toHid, player, applySettings, settings: () => settings, refresh };
"""


class Mock:
    """Backend stand-in. The driver edits .state; the page polls it."""

    def __init__(self) -> None:
        self.state = {"phase": "idle", "message": "", "udid": None, "device": {}, "usbmuxOk": True,
                      "devices": [], "features": {"ocr": True, "audio": True}, "audioDevice": None}
        self.settings = {"theme": "dark", "ctrlAsCmd": True, "scrollSpeed": 100, "reflection": True,
                         "autoUpdateCheck": False, "allowWifi": False, "railCompact": False}
        self.posts: list[tuple[str, dict]] = []


def serve(root: Path, mock: Mock) -> ThreadingHTTPServer:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *_):
            pass

        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/state":
                return self._json(mock.state)
            if self.path == "/api/settings":
                return self._json(mock.settings)
            if self.path == "/api/update":
                return self._json({"current": "sim", "supported": True, "reason": "", "available": None})
            if self.path.startswith("/api/audio/devices"):
                return self._json({"devices": [], "active": None})
            return super().do_GET()

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                body = {}
            mock.posts.append((self.path, body))
            if self.path == "/api/settings":
                mock.settings.update(body)
                return self._json(mock.settings)
            if self.path.startswith("/m/"):
                if self.path == "/m/rotate":
                    return self._json({"currentDeviceOrientation": "portrait"})
                return self._json({"ok": True})
            return self._json({"ok": True, "chars": 12, "lines": 1})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def prepare() -> Path:
    root = Path(tempfile.mkdtemp(prefix="portunus-sim-"))
    for f in UI.iterdir():
        shutil.copy(f, root / f.name)
    (root / "stub.js").write_text(STUB_JS)
    with open(root / "app.js", "a", encoding="utf-8") as js:
        js.write(HOOKS_JS)
    html = (root / "index.html").read_text()
    html = html.replace('<script type="module" src="app.js"></script>',
                        '<script src="stub.js"></script>\n  <script type="module" src="app.js"></script>')
    (root / "index.html").write_text(html)
    return root


def browser():
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service

    opts = Options()
    opts.add_argument("-headless")
    opts.binary_location = os.environ.get("FIREFOX", "firefox")
    return webdriver.Firefox(options=opts, service=Service(os.environ.get("GECKODRIVER", "geckodriver")))


def set_viewport(driver, w: int, h: int) -> None:
    driver.set_window_size(w, h)
    iw, ih = driver.execute_script("return [innerWidth, innerHeight]")
    driver.set_window_size(w + (w - iw), h + (h - ih))


MEASURE = """
const c = document.getElementById('canvas'), s = document.getElementById('screen');
const r = c.getBoundingClientRect(), b = s.getBoundingClientRect();
const tl = window.__ui.toHid(r.left + 0.5, r.top + 0.5), br = window.__ui.toHid(r.right - 0.5, r.bottom - 0.5);
return {cw: r.width, ch: r.height, cl: r.left, ct: r.top, cr: r.right, cb: r.bottom,
        sl: b.left, st: b.top, sr: b.right, sb: b.bottom, bw: c.width, bh: c.height,
        tl, br, scrollX: document.documentElement.scrollWidth > innerWidth,
        railScroll: (() => { const r = document.querySelector('.rail');
          return getComputedStyle(r).flexDirection === 'column' && r.scrollHeight > r.clientHeight + 1; })()};
"""


def go_live(driver, mock: Mock) -> None:
    mock.state.update(phase="live", device={"name": "iPhone", "transport": "usb"})
    deadline = time.time() + 10
    while time.time() < deadline:
        if driver.execute_script("return !document.getElementById('live').hidden && window.__ui?.player.running"):
            return
        time.sleep(0.1)
    raise RuntimeError("UI did not reach the live view")


RAIL_STATE = """
const ind = document.getElementById('rail-indicator'), tip = document.getElementById('tip');
const b = arguments[0].getBoundingClientRect(), i = ind.getBoundingClientRect();
return {on: ind.classList.contains('on'), dx: Math.abs(i.left - b.left), dy: Math.abs(i.top - b.top),
        dw: Math.abs(i.width - b.width), tip: !tip.hidden, label: tip.querySelector('.tip-label').textContent,
        keys: [...tip.querySelectorAll('kbd')].map(k => k.textContent), note: tip.querySelector('.tip-note').textContent,
        railW: document.getElementById('rail').getBoundingClientRect().width};
"""


def check_rail(driver, mock: Mock) -> list[str]:
    """Interaction checks for the sidebar, offline and live."""
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By

    problems = []
    set_viewport(driver, 1120, 1000)
    mock.state.update(phase="idle", device={})
    time.sleep(1.3)
    lock = driver.find_element(By.CSS_SELECTOR, '[data-action="lock"]')
    ActionChains(driver).move_to_element(lock).perform()
    time.sleep(0.75)
    st = driver.execute_script(RAIL_STATE, lock)
    if not (st["tip"] and st["label"] == "Kunci iPhone" and st["note"] == "Sambungkan iPhone dulu"):
        problems.append(f"tooltip tombol nonaktif salah: {st}")
    go_live(driver, mock)
    time.sleep(0.5)
    for action, label, keys in [("home", "Beranda", ["Ctrl", "Shift", "H"]), ("rotate-right", "Putar kanan", ["Ctrl", "Shift", "]"]),
                                ("shot", "Simpan tangkapan layar", ["Ctrl", "Shift", "S"]), ("settings", "Pengaturan", [])]:
        btn = driver.find_element(By.CSS_SELECTOR, f'[data-action="{action}"]')
        ActionChains(driver).move_to_element(btn).perform()
        time.sleep(0.75)
        st = driver.execute_script(RAIL_STATE, btn)
        if not st["on"] or max(st["dx"], st["dy"], st["dw"]) > 2:
            problems.append(f"sorotan tidak menempel di {action}: {st}")
        if not st["tip"] or st["label"] != label or st["keys"] != keys or st["note"]:
            problems.append(f"tooltip {action} salah: {st}")
    shot = driver.find_element(By.CSS_SELECTOR, '[data-action="shot"]')
    shot.click()
    time.sleep(0.4)
    icon = driver.execute_script("return [arguments[0].classList.contains('done'), arguments[0].querySelector('use').getAttribute('href')]", shot)
    if icon != [True, "#i-check"]:
        problems.append(f"kilasan sukses tidak muncul: {icon}")
    time.sleep(1.1)
    if driver.execute_script("return arguments[0].querySelector('use').getAttribute('href')", shot) != "#i-shot":
        problems.append("ikon tidak kembali setelah kilasan")
    wide = driver.execute_script(RAIL_STATE, shot)["railW"]
    driver.find_element(By.CSS_SELECTOR, '[data-action="rail-toggle"]').click()
    time.sleep(0.4)
    narrow = driver.execute_script(RAIL_STATE, shot)["railW"]
    toggle = driver.find_element(By.CSS_SELECTOR, '[data-action="rail-toggle"]')
    st = driver.execute_script(RAIL_STATE, toggle)
    if st["on"] and max(st["dx"], st["dy"]) > 2:
        problems.append(f"sorotan tertinggal setelah sidebar diciutkan: {st}")
    if not (wide > 90 and narrow < 72 and mock.settings.get("railCompact") is True):
        problems.append(f"ciutkan sidebar gagal: {wide} -> {narrow}, setelan {mock.settings.get('railCompact')}")
    driver.find_element(By.CSS_SELECTOR, '[data-action="rail-toggle"]').click()
    time.sleep(0.3)
    n_vol = len([p for p in mock.posts if p[0] == "/m/button"])
    vol = driver.find_element(By.CSS_SELECTOR, '[data-action="volume-up"]')
    ActionChains(driver).click_and_hold(vol).pause(1.1).release().perform()
    time.sleep(0.2)
    presses = len([p for p in mock.posts if p[0] == "/m/button"]) - n_vol
    if presses < 4:
        problems.append(f"tahan volume tidak mengulang ({presses}x)")
    return problems


def check_stream(driver) -> list[str]:
    problems = []
    set_viewport(driver, 1120, 1000)
    driver.execute_script("window.__device = {w: 1179, h: 2556, label: 'collapse', collapse: true}")
    time.sleep(0.6)
    r = driver.execute_script("""
      const c = document.getElementById('canvas'), g = c.getContext('2d');
      const px = g.getImageData(c.width - 40, c.height - 300, 1, 1).data;
      return {crop: window.__ui.player.crop, corner: [px[0], px[1], px[2]]};""")
    if not r["crop"] or abs(r["crop"][0] - 707) > 12 or abs(r["crop"][1] - 1534) > 24:
        problems.append(f"collapse tidak terdeteksi: {r}")
    if all(abs(v - 128) < 6 for v in r["corner"]):
        problems.append(f"padding abu-abu masih tampil: {r}")
    driver.execute_script("window.__device = {w: 1179, h: 2556, label: 'normal'}")
    time.sleep(0.4)
    if driver.execute_script("return window.__ui.player.crop") is not None:
        problems.append("frame normal salah dianggap collapse")
    driver.execute_script("document.dispatchEvent(new KeyboardEvent('keydown', {code: 'KeyD', ctrlKey: true, shiftKey: true, bubbles: true}))")
    time.sleep(0.6)
    text = driver.execute_script("return document.querySelector('.diag')?.textContent || ''")
    if "frame   1179x2556" not in text or "canvas  1179x2556" not in text:
        problems.append(f"panel diagnostik salah: {text!r}")
    driver.execute_script("document.dispatchEvent(new KeyboardEvent('keydown', {code: 'KeyD', ctrlKey: true, shiftKey: true, bubbles: true}))")
    return problems


def run_tests() -> int:
    root, mock = prepare(), Mock()
    server = serve(root, mock)
    driver = browser()
    failures, worst, total = [], 0.0, 0
    try:
        driver.get(f"http://127.0.0.1:{server.server_port}/")
        rail_problems = check_rail(driver, mock)
        print(f"uji interaksi sidebar: {'semua lolos' if not rail_problems else 'GAGAL'}")
        for p in rail_problems:
            print("  GAGAL", p)
        failures.extend(rail_problems)
        go_live(driver, mock)
        extra = check_stream(driver)
        print(f"uji collapse + diagnostik: {'semua lolos' if not extra else 'GAGAL'}")
        for p in extra:
            print("  GAGAL", p)
        failures.extend(extra)
        for name, pw, ph in DEVICES:
            for land in (False, True):
                w, h = (ph, pw) if land else (pw, ph)
                driver.execute_script("window.__device = arguments[0]", {"w": w, "h": h, "label": name, "android": "Android" in name})
                for vw, vh in WINDOWS:
                    set_viewport(driver, vw, vh)
                    for reflect in (True, False):
                        driver.execute_script("window.__ui.settings().reflection = arguments[0]; window.__ui.applySettings();", reflect)
                        time.sleep(0.12)
                        m = driver.execute_script(MEASURE)
                        total += 1
                        problems = []
                        if (m["bw"], m["bh"]) != (w, h):
                            problems.append(f"canvas buffer {m['bw']}x{m['bh']}")
                        err = abs((m["cw"] / m["ch"]) / (w / h) - 1)
                        worst = max(worst, err)
                        if err > 0.01:
                            problems.append(f"rasio meleset {err:.1%}")
                        if m["cl"] < m["sl"] - 1 or m["cr"] > m["sr"] + 1 or m["ct"] < m["st"] - 1 or m["cb"] > m["sb"] + 1:
                            problems.append("keluar area")
                        if reflect and m["cb"] + 6 + 0.10 * m["ch"] > m["sb"] + 24 + 1:
                            problems.append("pantulan meluber")
                        if max(m["tl"]["x"], m["tl"]["y"]) > 700 or min(m["br"]["x"], m["br"]["y"]) < 64835:
                            problems.append(f"sentuhan sudut meleset {m['tl']} {m['br']}")
                        if m["scrollX"]:
                            problems.append("scroll horizontal")
                        if m["railScroll"]:
                            problems.append("rail butuh scroll")
                        if problems:
                            failures.append(f"{name} {'miring' if land else 'tegak'} {vw}x{vh} pantulan={reflect}: {', '.join(problems)}")
    finally:
        driver.quit()
        server.shutdown()
        shutil.rmtree(root, ignore_errors=True)
    print(f"{total} kasus, {total - len(failures)} lolos, {len(failures)} gagal, selisih rasio terburuk {worst:.3%}")
    for f in failures[:30]:
        print("  GAGAL", f)
    return 1 if failures else 0


def encode(frames, frames_dir: Path, out: Path) -> None:
    """Frames keep their real timing: each lasts until the next was captured."""
    listing = frames_dir / "frames.txt"
    with open(listing, "w") as f:
        for (path, ts), (_, nxt) in zip(frames, frames[1:] + [(None, frames[-1][1] + 0.1)]):
            f.write(f"file '{path}'\nduration {max(0.02, nxt - ts):.3f}\n")
        f.write(f"file '{frames[-1][0]}'\n")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-vf", "scale=1120:760:force_original_aspect_ratio=decrease,pad=1120:760:(ow-iw)/2:(oh-ih)/2:color=0x121619,fps=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)], check=True)
    shutil.rmtree(frames_dir, ignore_errors=True)


def run_rail_video(out: Path) -> int:
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By

    root, mock = prepare(), Mock()
    server = serve(root, mock)
    driver = browser()
    frames_dir = Path(tempfile.mkdtemp(prefix="portunus-frames-"))
    frames: list[tuple[Path, float]] = []

    def grab(seconds: float) -> None:
        end = time.time() + seconds
        while True:
            path = frames_dir / f"{len(frames):05d}.png"
            driver.save_screenshot(str(path))
            frames.append((path, time.time()))
            if time.time() >= end:
                break

    def caption(text: str) -> None:
        driver.execute_script("window.__caption(arguments[0])", text)

    def hover(action: str, hold: float = 0.55) -> None:
        ActionChains(driver).move_to_element(driver.find_element(By.CSS_SELECTOR, f'[data-action="{action}"]')).perform()
        grab(hold)

    try:
        set_viewport(driver, 1120, 1000)
        driver.get(f"http://127.0.0.1:{server.server_port}/")
        time.sleep(1.2)
        mock.state["devices"] = [{"udid": "00008120-001A32801E", "name": "Ripan", "ios": "27.2", "transport": "usb", "wifiReady": False}]
        caption("Belum tersambung: kepala sidebar OFFLINE, tombol iPhone redup")
        grab(1.6)
        caption("Arahkan ke tombol redup: tooltip menjelaskan alasannya")
        hover("lock", 1.8)
        caption("Menghubungkan: ikon ponsel di kepala berdenyut")
        for phase in ("pairing", "tunnel", "starting"):
            mock.state.update(phase=phase, message="")
            grab(0.7)
        mock.state.update(phase="live", device={"name": "Ripan", "transport": "usb"})
        caption("Tersambung: titik hijau berdenyut, jalur USB tampil")
        ActionChains(driver).move_by_offset(400, 0).perform()
        grab(1.6)
        caption("Sorotan meluncur mengikuti kursor, tooltip berisi nama + shortcut")
        for action in ("home", "lock", "volume-up", "volume-down", "rotate-left", "rotate-right", "paste", "copy", "ocr", "shot"):
            hover(action, 0.5)
        caption("Ikon bergerak sesuai fungsinya: putar, volume, gear")
        for action in ("rotate-left", "rotate-right", "settings"):
            hover(action, 0.7)
        caption("Aksi selesai: ikon berubah jadi tanda centang")
        ActionChains(driver).move_to_element(driver.find_element(By.CSS_SELECTOR, '[data-action="shot"]')).click().perform()
        grab(1.6)
        caption("Tahan tombol volume untuk mengulang, seperti tombol fisik")
        vol = driver.find_element(By.CSS_SELECTOR, '[data-action="volume-up"]')
        ActionChains(driver).click_and_hold(vol).perform()
        grab(1.4)
        ActionChains(driver).release().perform()
        caption("Ciutkan sidebar: hanya ikon, nama tetap ada di tooltip")
        driver.find_element(By.CSS_SELECTOR, '[data-action="rail-toggle"]').click()
        grab(0.8)
        hover("paste", 1.2)
        hover("shot", 0.8)
        caption("Lebarkan lagi, pilihan disimpan")
        driver.find_element(By.CSS_SELECTOR, '[data-action="rail-toggle"]').click()
        grab(1.2)
        caption("Jendela pendek: sidebar otomatis ringkas tanpa scroll")
        set_viewport(driver, 1120, 640)
        grab(1.6)
        caption("")
        grab(0.3)
    finally:
        driver.quit()
        server.shutdown()
        shutil.rmtree(root, ignore_errors=True)
    encode(frames, frames_dir, out)
    print(f"video: {out} ({len(frames)} frame)")
    return 0


def run_video(out: Path) -> int:
    root, mock = prepare(), Mock()
    server = serve(root, mock)
    driver = browser()
    frames_dir = Path(tempfile.mkdtemp(prefix="portunus-frames-"))
    frames: list[tuple[Path, float]] = []

    def grab(seconds: float) -> None:
        end = time.time() + seconds
        while True:
            path = frames_dir / f"{len(frames):05d}.png"
            driver.save_screenshot(str(path))
            frames.append((path, time.time()))
            if time.time() >= end:
                break

    def caption(text: str) -> None:
        driver.execute_script("window.__caption(arguments[0])", text)

    def device(name: str, w: int, h: int) -> None:
        driver.execute_script("window.__device = arguments[0]", {"w": w, "h": h, "label": name, "android": "Android" in name})

    usb = {"udid": "00008120-001A32801E", "name": "Ripan", "ios": "27.2", "transport": "usb", "wifiReady": False}
    wifi_dev = {"udid": "00008110-000B77C11F", "name": "iPhone Kantor", "ios": "27.1", "transport": "wifi", "wifiReady": True}
    try:
        set_viewport(driver, 1120, 760)
        driver.get(f"http://127.0.0.1:{server.server_port}/")
        time.sleep(1.2)
        mock.state["devices"] = [usb]
        caption("1. Layar awal: iPhone tersambung lewat USB terdeteksi")
        grab(2.2)
        caption("2. Pengaturan: izinkan sambungan Wi-Fi (default mati)")
        driver.execute_script("document.querySelector('[data-action=settings]').click()")
        grab(1.0)
        driver.execute_script("const f=document.querySelector('#settings form'); f.allowWifi.checked=true; f.allowWifi.dispatchEvent(new Event('change',{bubbles:true}));")
        grab(1.4)
        driver.execute_script("document.querySelector('[data-close]').click()")
        caption("3. Tekan Aktifkan Wi-Fi (sekali, saat iPhone dicolok)")
        grab(1.4)
        driver.execute_script("[...document.querySelectorAll('#devices button')].find(b => b.textContent === 'Aktifkan Wi-Fi').click()")
        usb["wifiReady"] = True
        mock.state["devices"] = [usb, wifi_dev]
        grab(1.6)
        caption("4. Izin Wi-Fi aktif: iPhone berlabel Wi-Fi bisa disambungkan tanpa kabel")
        grab(2.0)
        caption("5. Menghubungkan: progres tiap tahap")
        mock.state.update(udid=usb["udid"])
        for phase, msg in [("pairing", "Menyapa iPhone."), ("tunnel", "Membuka jalur data ke iPhone."),
                           ("mounting", "Memasang Developer Disk Image."), ("starting", "Menyalakan siaran layar.")]:
            mock.state.update(phase=phase, message=msg)
            grab(0.9)
        device("iPhone 15", 1179, 2556)
        mock.state.update(phase="live", device={"name": "Ripan", "transport": "usb"})
        caption("6. Live: layar iPhone 15 (1179x2556) muat utuh, rasio tepat")
        grab(2.0)
        caption("7. Klik = sentuhan, ada riak di titik sentuh")
        for fx, fy in [(0.3, 0.3), (0.7, 0.45), (0.5, 0.75)]:
            driver.execute_script("""
              const c=document.getElementById('canvas').getBoundingClientRect();
              const x=c.left+c.width*arguments[0], y=c.top+c.height*arguments[1];
              const o={bubbles:true,clientX:x,clientY:y,button:0,pointerId:1};
              document.getElementById('canvas').dispatchEvent(new PointerEvent('pointerdown',o));
              document.getElementById('canvas').dispatchEvent(new PointerEvent('pointerup',o));""", fx, fy)
            grab(0.45)
        caption("8. Mode ketik: chip menandai keyboard PC sedang dikirim ke iPhone")
        driver.execute_script("document.getElementById('screen').focus()")
        grab(1.6)
        driver.execute_script("document.getElementById('screen').blur()")
        caption("9. iPhone diputar: layar miring tetap pas")
        device("iPhone 15", 2556, 1179)
        grab(2.0)
        caption("10. Jendela diperkecil: ukuran ikut menyesuaikan")
        set_viewport(driver, 760, 640)
        grab(1.4)
        set_viewport(driver, 1120, 760)
        caption("11. Uji rasio lain: iPhone SE, Pro Max, dan rasio Android")
        for name, w, h in [("iPhone SE", 750, 1334), ("iPhone 15 Pro Max", 1290, 2796),
                           ("Pixel 8 (rasio Android)", 1080, 2400), ("Galaxy Z Fold (rasio Android)", 1812, 2176)]:
            device(name, w, h)
            grab(1.2)
        device("iPhone 15", 1179, 2556)
        caption("12. Sambungan Wi-Fi: status bar menunjukkan jalurnya")
        mock.state.update(device={"name": "iPhone Kantor", "transport": "wifi"})
        grab(2.0)
        caption("13. Resolution collapse (gerakan cepat): gambar kecil diregangkan otomatis")
        driver.execute_script("window.__device = {w: 1179, h: 2556, label: 'collapse 60%', collapse: true}")
        grab(1.6)
        caption("14. Ctrl+Shift+D: panel diagnostik untuk laporan masalah")
        driver.execute_script("document.dispatchEvent(new KeyboardEvent('keydown', {code: 'KeyD', ctrlKey: true, shiftKey: true, bubbles: true}))")
        grab(2.2)
        driver.execute_script("document.dispatchEvent(new KeyboardEvent('keydown', {code: 'KeyD', ctrlKey: true, shiftKey: true, bubbles: true}))")
        device("iPhone 15", 1179, 2556)
        caption("15. Notifikasi dan kilasan ikon untuk setiap aksi")
        driver.execute_script("document.querySelector('[data-action=shot]').click()")
        grab(1.8)
        caption("")
        grab(0.4)
    finally:
        driver.quit()
        server.shutdown()
        shutil.rmtree(root, ignore_errors=True)

    encode(frames, frames_dir, out)
    print(f"video: {out} ({len(frames)} frame)")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["test"]:
        raise SystemExit(run_tests())
    if sys.argv[1:2] == ["video-rail"] and len(sys.argv) == 3:
        raise SystemExit(run_rail_video(Path(sys.argv[2])))
    if sys.argv[1:2] == ["video"] and len(sys.argv) == 3:
        raise SystemExit(run_video(Path(sys.argv[2])))
    raise SystemExit(__doc__)
