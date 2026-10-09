# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "soundfile", "lameenc"]
# ///
"""Trim takes in the browser and save them as named songs.

    uv run trim_takes.py 2026-10-08/takes
    uv run trim_takes.py --format mp3 2026-10-08/takes
    uv run trim_takes.py --resave 2026-10-08/takes

Opens a page listing every take in the folder. For each one: drag the two
handles to where the song starts and ends, type its name, and save. Takes
recorded by more than one mic (from pair_mics.py) are shown as one take: you
can switch mics while listening, and one trim is applied to every mic's file.

Songs are written to a "songs" folder beside the takes folder, each brought to
the same loudness (see --loudness) with a limiter so nothing clips. The takes
themselves are never changed. Press Ctrl-C here to stop.

--resave exports every song saved so far again, with the same crop and name,
and exits. Use it after changing the format or loudness, or after adding a mic.
"""
import argparse
import json
import re
import subprocess
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import lameenc
import numpy as np
import soundfile as sf

PEAKS_PER_SECOND = 100
FADE_IN, FADE_OUT = 0.01, 0.03  # seconds, just enough to avoid clicks at the cuts
TAKE_NAME = re.compile(r"^(\d+ .+? - \d+m\d\ds)(?: - (.+))?$")

folder = out = None
options = None
peaks_cache = {}
lock = threading.Lock()


def state_file():
    return out / ".trim.json"


def load_state():
    try:
        return json.loads(state_file().read_text())
    except (OSError, ValueError):
        return {}


def list_takes():
    """Group the folder's .wav files into takes, one entry per mic."""
    state, takes = load_state(), {}
    for path in sorted(folder.glob("*.wav")):
        match = TAKE_NAME.match(path.stem)
        key, mic = (match.group(1), match.group(2) or "") if match else (path.stem, "")
        take = takes.setdefault(key, {"id": key, "mics": [], "duration": 0.0, "saved": state.get(key)})
        duration = sf.info(path).duration
        take["mics"].append({"mic": mic, "file": path.name, "duration": duration})
        take["duration"] = max(take["duration"], duration)
    return list(takes.values())


def peaks(name):
    """Loudest sample in each 1/100 s of a file, 0-255."""
    if name not in peaks_cache:
        with sf.SoundFile(folder / name) as f:
            hop = round(f.samplerate / PEAKS_PER_SECOND)
            mono = np.abs(f.read(dtype="float32", always_2d=True)).max(axis=1)
        mono = mono[: len(mono) // hop * hop].reshape(-1, hop).max(axis=1)
        peaks_cache[name] = np.minimum(mono * 255, 255).astype(int).tolist()
    return peaks_cache[name]


def loudness(audio, rate):
    """Perceived loudness in LUFS (ITU-R BS.1770), as heard with the file playing in both ears."""
    n = len(audio)
    z = np.exp(-2j * np.pi * np.fft.rfftfreq(n, 1 / rate) / 48000)  # the standard's filters, defined at 48 kHz
    shelf = (1.53512485958697 - 2.69169618940638 * z + 1.19839281085285 * z**2) / (1 - 1.69065929318241 * z + 0.73248077421585 * z**2)
    highpass = (1 - 2 * z + z**2) / (1 - 1.99004745483398 * z + 0.99007225036621 * z**2)
    weighted = np.fft.irfft(np.fft.rfft(audio, axis=0) * (shelf * highpass)[:, None], n, axis=0)
    block, hop = round(0.4 * rate), round(0.1 * rate)
    if n < block:
        return -70.0
    running = np.concatenate([np.zeros((1, audio.shape[1])), np.cumsum(weighted**2, axis=0)])
    starts = np.arange(0, n - block + 1, hop)
    power = ((running[starts + block] - running[starts]) / block).sum(axis=1)
    if audio.shape[1] == 1:
        power = power * 2  # a mono file plays in both ears
    level = lambda p: -0.691 + 10 * np.log10(p.mean()) if len(p) else -70.0
    power = power[power > 10 ** ((-70 + 0.691) / 10)]
    power = power[power > 10 ** ((level(power) - 10 + 0.691) / 10)]
    return float(level(power))


def limit(audio, rate, ceiling):
    """Turn the level down smoothly around any peak above the ceiling, so nothing clips."""
    need = np.minimum(1.0, ceiling / np.maximum(np.abs(audio).max(axis=1), 1e-9)).astype(np.float64)
    if need.min() >= 1.0:
        return audio
    window = 1 << int(np.ceil(np.log2(0.04 * rate)))  # about 40 ms, a power of two
    low = np.pad(need, window, constant_values=1.0)
    step = 1
    while step < window:  # running minimum, doubling its reach each pass
        low = np.minimum(low[:-step], low[step:])
        step *= 2
    # low[t] now covers the window ending just before sample t - window; averaging a window's
    # worth of these, centred on each sample, keeps every peak under the ceiling.
    total = np.concatenate([[0.0], np.cumsum(low)])
    n = len(need)
    gain = (total[window + 1 : window + 1 + n] - total[1 : 1 + n]) / window
    return audio * gain[:, None].astype(np.float32)


def master(audio, rate):
    """Bring a song to the target loudness; returns it with the gain applied in dB."""
    before = loudness(audio, rate)
    if options.loudness is None or before <= -60:
        return audio, 0.0
    ceiling = 10 ** (options.ceiling / 20)
    gain = options.loudness - before
    out = limit(audio * 10 ** (gain / 20), rate, ceiling)
    for _ in range(2):  # limiting takes a little loudness back off; make it up
        gain += options.loudness - loudness(out, rate)
        out = limit(audio * 10 ** (gain / 20), rate, ceiling)
    return out, gain


def encode(audio, rate, path):
    if path.suffix in (".flac", ".wav"):
        sf.write(path, audio, rate, subtype="PCM_24")
    elif path.suffix == ".mp3":
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(options.bitrate)
        encoder.set_in_sample_rate(rate)
        encoder.set_channels(audio.shape[1])
        encoder.set_quality(2)
        encoder.silence()
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
        path.write_bytes(bytes(encoder.encode(pcm.tobytes())) + bytes(encoder.flush()))
    else:  # m4a, with the encoder built into macOS
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "take.wav"
            sf.write(wav, audio, rate, subtype="PCM_24")
            subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-b", str(options.bitrate * 1000), str(wav), str(path)], check=True)


def save(request):
    take = next(t for t in list_takes() if t["id"] == request["id"])
    start, end = float(request["start"]), float(request["end"])
    title = re.sub(r'[/:\\]', "-", request.get("title", "")).strip() or take["id"]
    with lock:
        state = load_state()
        out.mkdir(exist_ok=True)
        for old in state.get(take["id"], {}).get("files", []):  # replacing an earlier save of this take
            (out / old).unlink(missing_ok=True)

        def names(base):
            several = len(take["mics"]) > 1
            return [f"{base} - {m['mic']}{options.suffix}" if several and m["mic"] else f"{base}{options.suffix}" for m in take["mics"]]

        base, n = title, 1
        while any((out / name).exists() for name in names(base)):  # another take of the same song
            n += 1
            base = f"{title} ({n})"
        written, gains = names(base), []
        for mic, name in zip(take["mics"], written):
            with sf.SoundFile(folder / mic["file"]) as f:
                rate = f.samplerate
                f.seek(round(start * rate))
                audio = f.read(round((end - start) * rate), dtype="float32", always_2d=True)
            for seconds, ramp in ((FADE_IN, slice(None)), (FADE_OUT, slice(None, None, -1))):
                n_fade = min(round(seconds * rate), len(audio))
                audio[ramp][:n_fade] *= np.linspace(0.0, 1.0, n_fade, dtype=np.float32)[:, None]
            audio, gain = master(audio, rate)
            gains.append(round(gain, 1))
            encode(audio, rate, out / name)
        saved = {"start": start, "end": end, "title": request.get("title", "").strip(), "files": written, "gain": gains}
        state[take["id"]] = saved
        state_file().write_text(json.dumps(state, indent=1))
    return saved


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, body, kind="application/json", status=200):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def take_file(self, name):
        """A .wav directly inside the takes folder, or None."""
        path = folder / name
        return path if path.suffix == ".wav" and path.parent == folder and path.is_file() else None

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/":
                self.reply(PAGE.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/takes":
                self.reply({"takes": list_takes(), "folder": str(folder), "out": str(out), "format": options.suffix[1:]})
            elif url.path == "/api/peaks" and self.take_file(parse_qs(url.query)["file"][0]):
                self.reply(peaks(parse_qs(url.query)["file"][0]))
            elif url.path.startswith("/audio/") and self.take_file(unquote(url.path[7:])):
                self.send_audio(self.take_file(unquote(url.path[7:])))
            else:
                self.reply({"error": "not found"}, status=404)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_audio(self, path):
        size = path.stat().st_size
        first, last = 0, size - 1
        ranged = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if ranged and (ranged.group(1) or ranged.group(2)):
            if ranged.group(1):
                first = int(ranged.group(1))
                last = min(int(ranged.group(2)), last) if ranged.group(2) else last
            else:
                first = max(size - int(ranged.group(2)), 0)
        self.send_response(206 if ranged else 200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(last - first + 1))
        if ranged:
            self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(first)
            left = last - first + 1
            while left > 0:
                chunk = f.read(min(1 << 18, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path != "/api/save":
            return self.reply({"error": "not found"}, status=404)
        try:
            self.reply({"saved": save(body)})
        except Exception as error:  # shown in the page
            self.reply({"error": str(error)}, status=500)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Trim takes</title>
<style>
  :root { --bg:#16181b; --panel:#1e2125; --line:#2c3036; --text:#e6e8ea; --dim:#8b929a; --accent:#3ec6a0; --warn:#e0a54a; }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body { background: var(--bg); color: var(--text); font: 14px/1.4 -apple-system, system-ui, sans-serif; display: flex; }
  aside { width: 290px; border-right: 1px solid var(--line); overflow-y: auto; flex: none; }
  aside div { padding: 7px 14px; cursor: pointer; border-bottom: 1px solid var(--line); }
  aside div:hover { background: var(--panel); }
  aside div.on { background: var(--panel); box-shadow: inset 3px 0 var(--accent); }
  aside small { color: var(--dim); display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  aside .done b { color: var(--accent); font-weight: 600; }
  main { flex: 1; min-width: 0; padding: 22px 26px; display: flex; flex-direction: column; gap: 14px; }
  h1 { font-size: 17px; margin: 0; font-weight: 600; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  input[type=text] { flex: 1; min-width: 200px; font: inherit; font-size: 18px; padding: 8px 11px; color: var(--text); background: var(--panel); border: 1px solid var(--line); border-radius: 7px; }
  input[type=text]:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
  button { font: inherit; color: var(--text); background: var(--panel); border: 1px solid var(--line); border-radius: 7px; padding: 7px 12px; cursor: pointer; }
  button:hover { border-color: var(--dim); }
  button.on { background: var(--accent); border-color: var(--accent); color: #0c1512; font-weight: 600; }
  button.go { background: var(--accent); border-color: var(--accent); color: #0c1512; font-weight: 600; }
  kbd { font: 11px ui-monospace, monospace; color: var(--dim); border: 1px solid var(--line); border-radius: 4px; padding: 0 4px; margin-left: 5px; }
  button.on kbd, button.go kbd { color: #0c1512; border-color: #0c151266; }
  canvas { width: 100%; height: 230px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; display: block; }
  .times { color: var(--dim); font-variant-numeric: tabular-nums; }
  .times b { color: var(--text); font-weight: 500; }
  #status { color: var(--accent); min-height: 1.4em; }
  #status.bad { color: var(--warn); }
  .help { color: var(--dim); font-size: 12.5px; line-height: 1.9; margin-top: auto; }
</style>
</head>
<body>
<aside id="list"></aside>
<main>
  <div class="row"><h1 id="name"></h1><span id="mics" class="row"></span></div>
  <div class="row">
    <input type="text" id="title" list="titles" autocomplete="off" spellcheck="false">
    <datalist id="titles"></datalist>
    <button class="go" id="save">Save &amp; next<kbd>Enter</kbd></button>
  </div>
  <canvas id="wave"></canvas>
  <div class="row times">
    <span>Start <b id="tStart"></b></span><span>End <b id="tEnd"></b></span><span>Length <b id="tLen"></b></span>
    <span style="margin-left:auto">Playhead <b id="tNow"></b></span>
  </div>
  <div class="row">
    <button id="fromStart">&#9654; Play from crop start<kbd>Q</kbd></button>
    <button id="play">Play / pause<kbd>Space</kbd></button>
    <button id="toEnd">Last 3 s of crop<kbd>E</kbd></button>
    <button id="setStart">Start here<kbd>I</kbd></button>
    <button id="setEnd">End here<kbd>O</kbd></button>
    <button id="zoom">Zoom to trim<kbd>Z</kbd></button>
  </div>
  <div id="status"></div>
  <div class="help">
    Drag the green handles, or click the waveform to move the playhead. Scroll to zoom, scroll sideways to pan, double-click to see the whole take.<br>
    <kbd>,</kbd> <kbd>.</kbd> nudge the handle you last moved by 0.1 s and replay it &nbsp; <kbd>←</kbd> <kbd>→</kbd> move 1 s (Shift: 5 s) &nbsp; <kbd>M</kbd> switch mic &nbsp; <kbd>↑</kbd> <kbd>↓</kbd> previous / next take &nbsp; <kbd>T</kbd> edit the name<br>
    <span id="where"></span>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
const canvas = $('wave'), ctx = canvas.getContext('2d');
const RATE = 100;
let takes = [], cur = -1, mic = 0, start = 0, end = 0, dur = 0, v0 = 0, v1 = 1;
let audios = [], peakData = {}, drag = null, lastHandle = 'start', stopAt = null, busy = false;

const fmt = t => { t = Math.max(t, 0); const m = Math.floor(t / 60); return m + ':' + (t - m * 60).toFixed(1).padStart(4, '0'); };
const player = () => audios[mic];
const now = () => player() ? player().currentTime : 0;
const playing = () => player() && !player().paused;

function say(text, bad) { $('status').textContent = text; $('status').className = bad ? 'bad' : ''; }

function renderList() {
  $('list').innerHTML = '';
  takes.forEach((t, i) => {
    const d = document.createElement('div');
    d.className = (i === cur ? 'on ' : '') + (t.saved ? 'done' : '');
    const mics = t.mics.map(m => m.mic).filter(Boolean).join(' + ');
    const b = document.createElement('b'); b.textContent = (t.saved ? '✓ ' : '') + t.id;
    const s = document.createElement('small'); s.textContent = t.saved ? (t.saved.title || t.id) + ' · ' + fmt(t.saved.end - t.saved.start) : mics;
    d.append(b, s);
    d.onclick = () => select(i);
    $('list').append(d);
  });
  const on = $('list').querySelector('.on'); if (on) on.scrollIntoView({block: 'nearest'});
  const known = [...new Set(takes.filter(t => t.saved && t.saved.title).map(t => t.saved.title))];
  $('titles').innerHTML = ''; known.forEach(k => { const o = document.createElement('option'); o.value = k; $('titles').append(o); });
}

function renderMics() {
  $('mics').innerHTML = '';
  const t = takes[cur];
  if (t.mics.length < 2) return;
  t.mics.forEach((m, j) => {
    const b = document.createElement('button');
    b.className = j === mic ? 'on' : '';
    b.innerHTML = '<kbd></kbd>'; b.prepend(m.mic || m.file); b.querySelector('kbd').textContent = j + 1;
    b.onclick = () => switchMic(j);
    $('mics').append(b);
  });
}

function select(i) {
  if (i < 0 || i >= takes.length) return;
  audios.forEach(a => { a.pause(); a.removeAttribute('src'); a.load(); });
  cur = i; stopAt = null;
  const t = takes[i];
  dur = t.duration;
  start = t.saved ? t.saved.start : 0;
  end = t.saved ? Math.min(t.saved.end, dur) : dur;
  v0 = 0; v1 = dur; lastHandle = 'start';
  mic = Math.min(mic, t.mics.length - 1);
  audios = t.mics.map(m => { const a = new Audio('/audio/' + encodeURIComponent(m.file)); a.preload = 'auto'; return a; });
  $('name').textContent = t.id;
  $('title').value = t.saved ? t.saved.title : '';
  $('title').placeholder = 'Song name (leave empty to keep "' + t.id + '")';
  say(t.saved ? 'Saved as ' + t.saved.files.join(', ') : '');
  renderMics(); renderList(); loadPeaks();
}

async function loadPeaks() {
  const file = takes[cur].mics[mic].file;
  if (!peakData[file]) peakData[file] = await (await fetch('/api/peaks?file=' + encodeURIComponent(file))).json();
}

function switchMic(j) {
  const t = takes[cur];
  if (j >= t.mics.length || j === mic) return;
  const at = now(), was = playing();
  player().pause();
  mic = j;
  player().currentTime = at;
  if (was) player().play();
  renderMics(); loadPeaks();
}

function seek(t) { if (player()) player().currentTime = Math.min(Math.max(t, 0), dur - 0.01); }
function playRange(from, to) { stopAt = to; seek(from); player().play(); }
function toggle() {
  if (playing()) return player().pause();
  if (now() < start - 0.05 || now() >= end - 0.05) seek(start);
  stopAt = end; player().play();
}
function setHandle(which, t) {
  if (which === 'start') start = Math.min(Math.max(t, 0), end - 0.5);
  else end = Math.max(Math.min(t, dur), start + 0.5);
  lastHandle = which;
  if (stopAt !== null) stopAt = end;
}
function audition() {
  if (lastHandle === 'start') playRange(start, end); else playRange(Math.max(start, end - 2), end);
}
function zoomToTrim() { const pad = Math.max((end - start) * 0.04, 1); v0 = Math.max(start - pad, 0); v1 = Math.min(end + pad, dur); }

const X = t => (t - v0) / (v1 - v0) * canvas.clientWidth;
const T = x => v0 + x / canvas.clientWidth * (v1 - v0);

function draw() {
  const w = canvas.clientWidth, h = canvas.clientHeight, r = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(w * r) || canvas.height !== Math.round(h * r)) { canvas.width = Math.round(w * r); canvas.height = Math.round(h * r); }
  ctx.setTransform(r, 0, 0, r, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (cur < 0) return;
  ctx.fillStyle = 'rgba(62,198,160,0.08)';
  ctx.fillRect(X(start), 0, X(end) - X(start), h);
  const p = peakData[takes[cur].mics[mic].file];
  if (p) {
    for (let x = 0; x < w; x++) {
      const a = Math.floor(T(x) * RATE), b = Math.max(Math.floor(T(x + 1) * RATE), a + 1);
      let peak = 0;
      for (let k = Math.max(a, 0); k < b && k < p.length; k++) if (p[k] > peak) peak = p[k];
      const t = T(x + 0.5), bar = Math.max(peak / 255 * (h - 16), 1);
      ctx.fillStyle = t >= start && t <= end ? '#3ec6a0' : '#4a5058';
      ctx.fillRect(x, (h - bar) / 2, 1, bar);
    }
  }
  for (const [t, label] of [[start, 'start'], [end, 'end']]) {
    const x = X(t);
    ctx.fillStyle = '#e6e8ea'; ctx.fillRect(x - 1, 0, 2, h);
    ctx.fillStyle = '#3ec6a0';
    ctx.beginPath();
    if (label === 'start') { ctx.moveTo(x, 0); ctx.lineTo(x + 12, 0); ctx.lineTo(x, 16); }
    else { ctx.moveTo(x, 0); ctx.lineTo(x - 12, 0); ctx.lineTo(x, 16); }
    ctx.fill();
  }
  ctx.fillStyle = '#e0a54a'; ctx.fillRect(X(now()) - 0.5, 0, 1.5, h);
  $('tStart').textContent = fmt(start); $('tEnd').textContent = fmt(end);
  $('tLen').textContent = fmt(end - start); $('tNow').textContent = fmt(now());
}

function frame() {
  if (playing() && stopAt !== null && now() >= stopAt) { player().pause(); player().currentTime = stopAt; stopAt = null; }
  draw();
  requestAnimationFrame(frame);
}

function near(e) {
  const x = e.offsetX;
  const ds = Math.abs(x - X(start)), de = Math.abs(x - X(end));
  if (Math.min(ds, de) > 9) return null;
  return ds <= de ? 'start' : 'end';
}
canvas.addEventListener('mousedown', e => { drag = near(e); if (!drag) { stopAt = null; seek(T(e.offsetX)); } });
window.addEventListener('mousemove', e => {
  const box = canvas.getBoundingClientRect();
  if (drag) setHandle(drag, T(e.clientX - box.left));
  else if (e.target === canvas) canvas.style.cursor = near(e) ? 'ew-resize' : 'text';
});
window.addEventListener('mouseup', () => { drag = null; });
canvas.addEventListener('dblclick', () => { v0 = 0; v1 = dur; });
canvas.addEventListener('wheel', e => {
  e.preventDefault();
  let span = v1 - v0;
  if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
    const move = e.deltaX / canvas.clientWidth * span;
    v0 += move; v1 += move;
  } else {
    const at = T(e.offsetX), k = Math.exp(e.deltaY * 0.004);
    span = Math.min(Math.max(span * k, 2), dur);
    v0 = at - (e.offsetX / canvas.clientWidth) * span; v1 = v0 + span;
  }
  if (v0 < 0) { v1 -= v0; v0 = 0; }
  if (v1 > dur) { v0 = Math.max(v0 - (v1 - dur), 0); v1 = dur; }
}, {passive: false});

async function saveTake() {
  if (cur < 0) return;
  if (busy) return say('Still saving the previous take\u2026');
  busy = true;
  if (player()) player().pause();
  const t = takes[cur], index = cur;
  say('Saving ' + t.id + '…');
  try {
    const res = await fetch('/api/save', {method: 'POST', body: JSON.stringify({id: t.id, start, end, title: $('title').value})});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'save failed');
    t.saved = data.saved;
    const done = 'Saved ' + data.saved.files.join(', ');
    if (cur === index) {  // still on the take that was saved; otherwise stay where the user went
      $('title').blur();
      if (cur + 1 < takes.length) select(cur + 1); else renderList();
    } else renderList();
    say(done);
  } catch (err) { say('Could not save: ' + err.message, true); }
  busy = false;
}

$('play').onclick = toggle;
$('fromStart').onclick = () => playRange(start, end);
$('toEnd').onclick = () => playRange(Math.max(start, end - 3), end);
$('setStart').onclick = () => setHandle('start', now());
$('setEnd').onclick = () => setHandle('end', now());
$('zoom').onclick = zoomToTrim;
$('save').onclick = saveTake;
document.querySelectorAll('button').forEach(b => b.addEventListener('mouseup', () => b.blur()));

window.addEventListener('keydown', e => {
  const typing = document.activeElement === $('title');
  if (e.key === 'Enter' || ((e.metaKey || e.ctrlKey) && e.key === 's')) { e.preventDefault(); return saveTake(); }
  if (typing) { if (e.key === 'Escape') $('title').blur(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const step = e.shiftKey ? 5 : 1;
  const act = {
    ' ': toggle,
    q: () => playRange(start, end),
    e: () => playRange(Math.max(start, end - 3), end),
    i: () => setHandle('start', now()),
    o: () => setHandle('end', now()),
    z: zoomToTrim,
    m: () => switchMic((mic + 1) % takes[cur].mics.length),
    t: () => $('title').focus(),
    ',': () => { setHandle(lastHandle, (lastHandle === 'start' ? start : end) - 0.1); audition(); },
    '.': () => { setHandle(lastHandle, (lastHandle === 'start' ? start : end) + 0.1); audition(); },
    ArrowLeft: () => seek(now() - step),
    ArrowRight: () => seek(now() + step),
    ArrowUp: () => select(cur - 1),
    ArrowDown: () => select(cur + 1),
  }[k];
  if (act) { e.preventDefault(); act(); }
  else if (/^[1-9]$/.test(k)) switchMic(+k - 1);
});

(async () => {
  const data = await (await fetch('/api/takes')).json();
  takes = data.takes;
  $('where').textContent = 'Takes: ' + data.folder + '  →  songs saved as .' + data.format + ' in ' + data.out;
  if (!takes.length) { say('No .wav files in ' + data.folder, true); return; }
  const first = takes.findIndex(t => !t.saved);
  select(first < 0 ? 0 : first);
  requestAnimationFrame(frame);
})();
</script>
</body>
</html>
"""


def main():
    global folder, out, options
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path, help="folder of takes (.wav)")
    parser.add_argument("--out", type=Path, help='where to save songs (default: a "songs" folder beside the takes folder)')
    parser.add_argument("--format", choices=["flac", "mp3", "m4a", "wav"], default="flac", help="format to save in (default flac, which is lossless)")
    parser.add_argument("--bitrate", type=int, default=192, help="kbps for mp3 and m4a (default 192)")
    parser.add_argument("--loudness", type=float, default=-14.0, help="loudness every saved song is brought to, in LUFS (default -14, about what streaming services use)")
    parser.add_argument("--no-master", dest="loudness", action="store_const", const=None, help="save songs at the level of the take, without matching loudness")
    parser.add_argument("--ceiling", type=float, default=-1.0, help="highest peak allowed after mastering, in dB (default -1)")
    parser.add_argument("--resave", action="store_true", help="export every saved song again with its saved crop and name, then exit")
    parser.add_argument("--port", type=int, default=8377)
    parser.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    options = parser.parse_args()
    options.suffix = "." + options.format
    folder = options.folder.resolve()
    out = (options.out or folder.parent / "songs").resolve()
    if options.resave:
        for take in list_takes():
            if take["saved"]:
                again = save({"id": take["id"], **{k: take["saved"][k] for k in ("start", "end", "title")}})
                print(f"{take['id']}  ->  {', '.join(again['files'])}")
        return
    try:
        server = ThreadingHTTPServer(("127.0.0.1", options.port), Handler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Trimming takes in {folder}\nSongs go to {out}\n\n  {url}\n\nCtrl-C to stop.")
    if not options.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
