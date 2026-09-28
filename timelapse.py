#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["opencv-python-headless", "numpy"]
# ///
"""Render cool 3d print timelapses from an RTSP camera and PrusaLink.
Uses OpenCV to pick the frame that shows the largest part of the print (with the print head
and the gantry as much out of the way as possible).

Usage: timelapse.py [command]
    capture  grab frames right now (per layer via PrusaLink, or per --interval)
    watch    wait for print jobs, capture each one per layer, render on finish
    render   build an mp4 or gif for a frames directory
    status   one PrusaLink status line

Configure PrusaLink by placing a file under ~/.config/print-timelapse.toml:
    [printer]
    host = 192.168....
    password = ... (get it from the xLCD display, network menu; user is hard-set to "maker")
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import tomllib

CONFIG = (
    Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    / "print-timelapse.toml"
)
DEFAULTS: dict[str, dict[str, Any]] = {
    "camera": {"url": "rtsp://192.168.4.133/live"},
    "printer": {"host": "192.168.4.101", "key": "", "password": "", "user": "maker"},
    "output": {"dir": str(Path(__file__).resolve().parent)},
    "web": {
        "host": "127.0.0.1",
        "port": 8811,
        "url": "",
    },  # url: public base for link previews
}
ACTIVE = {"PRINTING", "PAUSED", "ATTENTION", "BUSY"}
Z_STEP = 0.05  # minimum z rise that counts as a new layer
END_LIFT = 5
FIRST_LAYER_MAX = 1.0
STABLE_POLLS = 3
MIN_LAYERS = 3
MIN_FRAMES = 10
POSTER_WIDTH = 640
JOIN_GRACE = 600  # seconds into a job after which no start-up gate applies
GIF_WIDTH = 640
STREAM_FPS = 6
BG_SAMPLES = 40  # frames per layer
BG_DIFF = 0.8  # normalised grey difference that counts as covered
THUMB = "thumb.png"
META = "meta.json"
SLICER_KEYS = {
    "filament_colour", "filament_settings_id", "print_settings_id",
    "printer_settings_id", "first_layer_height", "fill_pattern", "perimeters",
    "top_solid_layers", "bottom_solid_layers", "support_material_style",
}  # fmt: skip


LOGFILE: Path | None = None


def log(msg: str) -> None:
    line = f"{dt.datetime.now().astimezone():%H:%M:%S} {msg}"
    print(line, file=sys.stderr, flush=True)
    if LOGFILE:
        with LOGFILE.open("a") as f:
            f.write(line + "\n")


def load_config() -> dict[str, dict[str, Any]]:
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if CONFIG.exists():
        for section, values in tomllib.loads(CONFIG.read_text()).items():
            cfg.setdefault(section, {}).update(values)
    return cfg


CFG = load_config()
BASE = Path(CFG["output"]["dir"])
FRAMES, LOGS, RENDER = BASE / "frames", BASE / "logs", BASE / "render"


def grab(url: str, dest: Path, timeout: float = 20) -> bool:
    tmp = dest.with_suffix(".part.jpg")
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-rtsp_transport", "tcp", "-i", url,
        "-frames:v", "1", "-q:v", "2", str(tmp),
    ]  # fmt: skip
    try:
        subprocess.run(cmd, timeout=timeout, check=True, capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        err = getattr(e, "stderr", b"") or b""
        log(f"grab failed: {err.decode(errors='replace').strip() or e}")
        tmp.unlink(missing_ok=True)
        return False
    tmp.rename(dest)
    return True


class Printer:
    def __init__(
        self, host: str, key: str = "", password: str = "", user: str = "maker"
    ):
        self.base = f"http://{host}"
        self.key = key
        self.password = password
        self.user = user
        self._nonce: str | None = None
        self._nc = 0

    def _digest(self, method: str, path: str, challenge: str) -> str:
        fields = dict(re.findall(r'(\w+)="?([^",]+)"?', challenge))
        realm, nonce = fields["realm"], fields["nonce"]

        def h(s: str) -> str:
            return hashlib.md5(s.encode()).hexdigest()

        ha1 = h(f"{self.user}:{realm}:{self.password}")
        ha2 = h(f"{method}:{path}")
        auth = f'Digest username="{self.user}", realm="{realm}", nonce="{nonce}", uri="{path}"'
        if "auth" not in fields.get("qop", ""):  # PrusaLink sends no qop
            return f'{auth}, response="{h(f"{ha1}:{nonce}:{ha2}")}"'
        if nonce != self._nonce:
            self._nonce, self._nc = nonce, 0
        self._nc += 1
        nc = f"{self._nc:08x}"
        cnonce = base64.b16encode(os.urandom(8)).decode().lower()
        resp = h(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        return f'{auth}, qop=auth, nc={nc}, cnonce="{cnonce}", response="{resp}"'

    def open(self, path: str, auth: str | None = None, timeout: float = 5) -> Any:
        req = urllib.request.Request(
            self.base + path, headers={"Accept": "application/json"}
        )
        if self.key:
            req.add_header("X-Api-Key", self.key)
        if auth:
            req.add_header("Authorization", auth)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            challenge = e.headers.get("WWW-Authenticate", "")
            if (
                e.code == 401
                and not auth
                and self.password
                and challenge.startswith("Digest")
            ):
                return self.open(path, self._digest("GET", path, challenge), timeout)
            raise

    def get(self, path: str) -> dict[str, Any]:
        with self.open(path) as r:
            return json.load(r) if r.status != 204 else {}

    def raw(self, path: str, timeout: float = 20) -> bytes:
        with self.open(path, timeout=timeout) as r:
            return r.read()

    def status(self) -> dict[str, Any]:
        return self.get("/api/v1/status")

    def job(self) -> dict[str, Any]:
        return self.get("/api/v1/job")


class Snap(NamedTuple):
    state: str
    job_id: int | None
    z: float | None
    progress: float | None
    time_printing: float | None
    time_remaining: float | None = None

    @property
    def active(self) -> bool:
        return self.state in ACTIVE and self.job_id is not None


def snapshot(printer: Printer) -> Snap | None:
    try:
        s = printer.status()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log(f"printer unreachable: {e}")
        return None
    p, j = s.get("printer", {}), s.get("job") or {}
    return Snap(
        p.get("state", "?"),
        j.get("id"),
        p.get("axis_z"),
        j.get("progress"),
        j.get("time_printing"),
        j.get("time_remaining"),
    )


def job_info(printer: Printer) -> dict[str, Any]:
    try:
        return printer.job()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log(f"job info failed: {e}")
        return {}


def job_name(printer: Printer) -> str:
    return (job_info(printer).get("file") or {}).get("display_name") or ""


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def bgcode_meta(stream: Any) -> tuple[dict[str, str], bytes | None]:
    header = stream.read(10)
    if len(header) < 10 or header[:4] != b"GCDE":
        raise ValueError("not a binary gcode file")
    _, _, checksum = struct.unpack("<4sIH", header)
    meta: dict[str, str] = {}
    png: bytes | None = None
    while True:
        head = stream.read(8)
        if len(head) < 8:
            break
        btype, comp, usize = struct.unpack("<HHI", head)
        if btype == 1:  # gcode
            break
        csize = struct.unpack("<I", stream.read(4))[0] if comp else usize
        params = stream.read(6 if btype == 5 else 2)
        data = stream.read(csize)
        stream.read(4 if checksum else 0)
        if comp == 1:
            data = zlib.decompress(data)
        elif comp:
            continue
        if btype == 5:
            fmt = struct.unpack("<HHH", params)[0]
            if fmt == 0 and (png is None or len(data) > len(png)):
                png = data
            continue
        for line in data.decode(errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and (btype != 2 or key in SLICER_KEYS):
                meta[key.strip()] = value.strip().strip('"')
    return meta, png


def fetch_gcode_meta(
    printer: Printer, download: str
) -> tuple[dict[str, str], bytes | None]:
    # PrusaLink refuses (404) the download while the file is being printed
    with printer.open(download, timeout=60) as r:
        return bgcode_meta(r)


def slug(name: str) -> str:
    # Drop technical part (nozzle, etc)
    name = re.split(r"_\d\.\dn_", Path(name).stem, maxsplit=1)[0]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:60] or "print"


class LayerTracker:
    def __init__(self, gate: bool = True) -> None:
        # The print is about to start, so ignore z until it settles
        self.gate = gate
        self.layer: float | None = None
        self.prev: float | None = None
        self.held = 0
        self.count = 0

    def update(self, z: float | None) -> str | None:
        # "layer" on a new layer, "end" on the end gcode lift, else None
        self.held = self.held + 1 if z == self.prev else 1
        self.prev = z
        if z is None or self.held < STABLE_POLLS:
            return None
        if self.layer is not None and z < self.layer - END_LIFT:
            # New print is about to start
            self.layer, self.gate = None, True
        if self.layer is None:
            # Homing, levelling, purging, blah
            if self.gate and not 0 < z < FIRST_LAYER_MAX:
                return None
            self.gate = True
            self.layer = z
            self.count = 1
            return "layer"
        if z > self.layer + END_LIFT:
            # Lift for last purge
            ended = self.count >= MIN_LAYERS
            self.layer, self.count = None, 0
            return "end" if ended else None
        if z > self.layer + Z_STEP:
            self.layer = z
            self.count += 1
            return "layer"
        return None


class Session:
    def __init__(self, camera: str, out: Path | str):
        global LOGFILE
        self.camera = camera
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.n = len(list(self.out.glob("[0-9]*.jpg")))
        self.last_t = 0.0
        LOGS.mkdir(parents=True, exist_ok=True)
        LOGFILE = LOGS / f"{self.out.name}.log"
        if "token" not in self.read_meta():
            self.meta(token=secrets.token_urlsafe(9))

    def read_meta(self) -> dict[str, Any]:
        return read_meta(self.out)

    def meta(self, **updates: Any) -> None:
        data = self.read_meta() | updates
        tmp = self.out / (META + ".part")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.rename(self.out / META)

    def close(self) -> None:
        global LOGFILE
        LOGFILE = None

    def grab(self, why: str = "") -> bool:
        started = time.monotonic()
        if grab(self.camera, self.out / f"{self.n:05d}.jpg"):
            self.last_t = started
            self.saved(why)
            return True
        return False

    def saved(self, why: str) -> None:
        log(f"{self.n:05d}.jpg {why}")
        self.n += 1


def read_meta(out: Path) -> dict[str, Any]:
    try:
        return json.loads((out / META).read_text())
    except (OSError, ValueError):
        return {}


def split_jpegs(buf: bytes) -> tuple[list[bytes], bytes]:
    jpgs = []
    while (end := buf.find(b"\xff\xd9")) >= 0:
        start = buf.find(b"\xff\xd8")
        if 0 <= start < end:
            jpgs.append(buf[start : end + 2])
        buf = buf[end + 2 :]
    return jpgs, buf


class Stream:
    def __init__(self, url: str, fps: float):
        self.url = url
        self.fps = fps
        self.proc: subprocess.Popen[bytes] | None = None
        self.stopped = False
        self.failures = 0

    def start(self) -> None:
        cmd = [
            "ffmpeg", "-nostdin", "-loglevel", "error",
            "-rtsp_transport", "tcp", "-i", self.url,
            "-vf", f"fps={self.fps}", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "2", "-",
        ]  # fmt: skip
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )

    def frames(self) -> Iterator[bytes]:
        buf = b""
        while not self.stopped:
            if self.proc is None or self.proc.poll() is not None:
                if self.proc is not None:
                    if self.failures % 30 == 0:
                        log(f"stream ended, restarting ({self.failures} failures)")
                    self.failures += 1
                    time.sleep(min(2 * self.failures, 60))
                self.start()
                buf = b""
            assert self.proc is not None and self.proc.stdout is not None
            chunk = self.proc.stdout.read(65536)
            if not chunk:
                continue
            buf += chunk
            jpgs, buf = split_jpegs(buf)
            if jpgs:
                self.failures = 0
            yield from jpgs

    def stop(self) -> None:
        self.stopped = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


class TrackedSession(Session):
    # Reads the stream continuously and keeps, per layer, the frame that differs least
    # from the previous layer's median image

    def __init__(self, camera: str, out: Path | str, gate: bool = True):
        super().__init__(camera, out)
        import cv2
        import numpy as np

        self.cv2, self.np = cv2, np
        self.layers = LayerTracker(gate)
        self.lock = threading.Lock()
        self.latest: bytes | None = None
        self.best: tuple[float, bytes, str] | None = None
        self.bg: Any = None
        self.samples: list[Any] = []
        self.seen = 0
        self.stream = Stream(camera, STREAM_FPS)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        for jpg in self.stream.frames():
            cost, info = self._cost(jpg)
            with self.lock:
                self.latest = jpg
                if self.best is None or cost < self.best[0]:
                    self.best = (cost, jpg, info)

    def _cost(self, jpg: bytes) -> tuple[float, str]:
        cv2, np = self.cv2, self.np
        gray = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_GRAYSCALE)
        small = cv2.resize(gray, (320, 180), interpolation=cv2.INTER_AREA).astype(
            np.float32
        )
        small = (small - small.mean()) / (small.std() + 1e-6)
        self.seen += 1
        if len(self.samples) < BG_SAMPLES:
            self.samples.append(small)
        elif random.random() < BG_SAMPLES / self.seen:
            self.samples[random.randrange(BG_SAMPLES)] = small
        if self.bg is None:
            self.bg = small
        covered = float((np.abs(small - self.bg) > BG_DIFF).mean())
        return covered, f"covered={covered:.2f}"

    def _write(self, jpg: bytes, why: str) -> bool:
        (self.out / f"{self.n:05d}.jpg").write_bytes(jpg)
        self.saved(why)
        return True

    def grab(self, why: str = "") -> bool:
        for _ in range(50):
            with self.lock:
                jpg = self.latest
            if jpg:
                return self._write(jpg, why)
            time.sleep(0.2)
        log("received no frame")
        return False

    def maybe_layer(self, z: float | None) -> str | None:
        event = self.layers.update(z)
        if not event:
            return None
        with self.lock:
            best, self.best = self.best, None
            latest = self.latest
            if self.samples:
                self.bg = self.np.median(self.np.stack(self.samples), axis=0)
                self.samples, self.seen = [], 0
        if best:
            _, jpg, info = best
            self._write(jpg, f"{event} z={z:.2f} {info}")
        elif latest and event == "layer":
            self._write(latest, f"z={z:.2f} no candidate")
        return event

    def close(self) -> None:
        self.stream.stop()
        super().close()


def make_session(camera: str, out: Path | str, snap: Snap | None) -> TrackedSession:
    gate = not (snap and snap.time_printing and snap.time_printing > JOIN_GRACE)
    return TrackedSession(camera, out, gate)


def record_start(sess: Session, printer: Printer, snap: Snap | None) -> None:
    job = job_info(printer)
    file = job.get("file") or {}
    sess.meta(
        started=now(),
        job_id=job.get("id"),
        state=job.get("state"),
        progress=job.get("progress"),
        time_printing=job.get("time_printing"),
        time_remaining=job.get("time_remaining"),
        file={k: file.get(k) for k in ("name", "display_name", "path", "size")},
        refs=file.get("refs") or {},
        joined_late=bool(
            snap and snap.time_printing and snap.time_printing > JOIN_GRACE
        ),
    )
    thumb = (file.get("refs") or {}).get("thumbnail")
    if thumb and not (sess.out / THUMB).exists():
        try:
            (sess.out / THUMB).write_bytes(printer.raw(thumb))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            log(f"thumbnail failed: {e}")


def record_progress(sess: TrackedSession, snap: Snap) -> None:
    sess.meta(
        state=snap.state,
        progress=snap.progress,
        time_printing=snap.time_printing,
        time_remaining=snap.time_remaining,
        layers=sess.layers.count,
        frames=sess.n,
    )


def record_end(sess: Session, printer: Printer, state: str | None) -> None:
    sess.meta(ended=now(), state=state, frames=sess.n)
    download = sess.read_meta().get("refs", {}).get("download")
    if not download:
        return
    try:
        gcode, png = fetch_gcode_meta(printer, download)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log(f"gcode metadata failed: {e}")
        return
    sess.meta(gcode=gcode)
    if png:
        (sess.out / THUMB).write_bytes(png)
    log(f"gcode metadata: {len(gcode)} keys" + (", thumbnail" if png else ""))


def capture_interval(
    camera: str, out: Path | str, interval: float, duration: float
) -> None:
    sess = Session(camera, out)
    end = time.monotonic() + duration if duration else None
    log(f"interval capture every {interval}s into {out}")
    while end is None or time.monotonic() < end:
        sess.grab()
        time.sleep(max(0, interval - (time.monotonic() - sess.last_t)))


def capture_layers(
    printer: Printer, camera: str, out: Path | str, poll: float, until_done: bool
) -> None:
    snap = snapshot(printer)
    sess = make_session(camera, out, snap)
    log(f"layer capture into {out}")
    if snap and snap.active:
        record_start(sess, printer, snap)
    sess.grab("start")
    while True:
        snap = snapshot(printer)
        if snap:
            if until_done and snap.state not in ACTIVE:
                log(f"printer {snap.state}, done: {sess.n} frames")
                sess.close()
                record_end(sess, printer, snap.state)
                return
            if sess.maybe_layer(snap.z):
                record_progress(sess, snap)
        time.sleep(poll)


def watch(
    printer: Printer,
    camera: str,
    outdir: Path | str,
    poll: float,
    fps: int,
    max_duration: int,
) -> None:
    log("watching for jobs")
    job_id: int | None = None
    sess: TrackedSession | None = None
    while True:
        snap = snapshot(printer)
        if snap:
            if snap.active and (sess is None or job_id != snap.job_id):
                if sess:
                    finish(sess, printer, None, fps, max_duration)
                name = slug(job_name(printer))
                out = (
                    Path(outdir)
                    / f"{dt.datetime.now().astimezone():%Y%m%d-%H%M}-{name}"
                )
                job_id, sess = snap.job_id, make_session(camera, out, snap)
                log(
                    f"job {snap.job_id} {name!r} started "
                    f"(progress {snap.progress}%, {snap.time_printing}s in, z={snap.z})"
                )
                record_start(sess, printer, snap)
                sess.grab("start")
            elif sess and not snap.active:
                log(f"job {job_id} ended ({snap.state})")
                finish(sess, printer, snap.state, fps, max_duration)
                sess = None
            elif sess:
                if sess.maybe_layer(snap.z):
                    record_progress(sess, snap)
        time.sleep(poll)


def finish(
    sess: Session, printer: Printer, state: str | None, fps: int, max_duration: int
) -> None:
    sess.close()
    if sess.n < MIN_FRAMES:
        log(f"{sess.out}: only {sess.n} frames, deleting")
        shutil.rmtree(sess.out, ignore_errors=True)
        return
    record_end(sess, printer, state)
    try:
        render(
            sess.out, RENDER / f"{sess.out.name}.mp4", fps, max_duration=max_duration
        )
        poster(sess.out, RENDER / f"{sess.out.name}.jpg")
    except subprocess.CalledProcessError as e:
        log(f"render failed: {e.stderr.decode(errors='replace')[-500:]}")


def poster(frames: Path, out: Path, video: Path | None = None) -> Path | None:
    files = sorted(Path(frames).glob("[0-9]*.jpg"))
    if files:
        src = ["-i", str(files[-1])]
    elif video and video.exists():
        src = ["-sseof", "-0.1", "-i", str(video)]
    else:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.jpg")
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y", *src,
        "-frames:v", "1", "-vf", f"scale={POSTER_WIDTH}:-2", "-q:v", "4", str(tmp),
    ]  # fmt: skip
    subprocess.run(cmd, check=True, capture_output=True)
    tmp.rename(out)
    return out


def thin(files: list[Path], limit: int) -> list[Path]:
    # Evenly spaced subset of at most limit frames, first and last kept
    if not limit or len(files) <= limit:
        return files
    step = (len(files) - 1) / (limit - 1)
    return [files[round(i * step)] for i in range(limit)]


def render(
    frames: Path | str,
    out: Path | str,
    fps: int = 30,
    hold: float = 2.0,
    crf: int = 25,
    max_duration: int = 20,
    gif: bool = False,
) -> Path:
    frames, out = Path(frames), Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not frames.is_dir():
        raise SystemExit(f"{frames}: no such directory")
    files = sorted(frames.glob("*.jpg"))
    if len(files) < 2:
        raise SystemExit(f"{frames}: {len(files)} frames, nothing to render")
    files = thin(files, fps * max_duration if max_duration else 0)
    concat = frames / "frames.txt"
    concat.write_text("".join(f"file '{f.name}'\n" for f in files))
    vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
    if hold:
        vf += f",tpad=stop_mode=clone:stop_duration={hold}"
    codec = ["-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-movflags", "+faststart"]  # fmt: skip
    if gif:
        vf += (
            f",scale={GIF_WIDTH}:-2:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
        )
        codec = ["-loop", "0"]
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y",
        "-r", str(fps), "-f", "concat", "-safe", "0", "-i", str(concat),
        "-filter_complex", vf, *codec, str(out),
    ]  # fmt: skip
    subprocess.run(cmd, check=True, capture_output=True)
    concat.unlink()
    log(f"{out} ({len(files)} frames, {len(files) / fps + hold:.0f}s)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--camera", default=CFG["camera"]["url"])
    ap.add_argument("--host", default=CFG["printer"]["host"])
    ap.add_argument("--key", default=CFG["printer"]["key"], help="PrusaLink API key")
    ap.add_argument(
        "--password", default=CFG["printer"]["password"], help="PrusaLink password"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="Grab frames now")
    c.add_argument("--out", help="frames directory (default: frames/<timestamp>)")
    c.add_argument(
        "--interval",
        type=float,
        help="seconds between frames (without it: per layer via PrusaLink)",
    )
    c.add_argument(
        "--duration", type=float, default=0, help="stop after N seconds (interval mode)"
    )
    c.add_argument(
        "--until-done",
        action="store_true",
        help="stop when the printer is no longer marked as printing or paused",
    )
    c.add_argument("--poll", type=float, default=2, help="PrusaLink poll interval")

    w = sub.add_parser(
        "watch", help="Run as daemon, create one timelapse per print job"
    )
    w.add_argument("--outdir", default=str(FRAMES))
    w.add_argument("--fps", type=int, default=30)
    w.add_argument(
        "--max-duration",
        type=int,
        default=20,
        help="seconds; drops frames evenly to fit, 0 = keep all",
    )
    w.add_argument("--poll", type=float, default=2, help="PrusaLink poll interval")

    r = sub.add_parser("render", help="Render an mp4")
    r.add_argument("frames")
    r.add_argument("--out", help="default: render/<frames dir name>.mp4 or .gif")
    r.add_argument(
        "--gif",
        action="store_true",
        help=f"{GIF_WIDTH}px wide looping gif instead of mp4",
    )
    r.add_argument("--fps", type=int, help="default 30, gif 10")
    r.add_argument(
        "--hold",
        type=float,
        help="seconds to freeze the last frame for a less abrupt ending; default 2, gif 1",
    )
    r.add_argument(
        "--crf", type=int, default=25, help="x264 quality, +3 halves the size"
    )
    r.add_argument(
        "--max-duration",
        type=int,
        help="seconds; drops frames evenly to fit, 0 = keep all; default 20, gif 3",
    )

    sub.add_parser("status", help="Print PrusaLink status")

    a = ap.parse_args()
    printer = Printer(a.host, a.key, a.password, CFG["printer"]["user"])
    needs_printer = a.cmd == "watch" or (a.cmd == "capture" and not a.interval)
    if needs_printer and not (a.key or a.password):
        raise SystemExit(f"{a.cmd} needs --key or --password")

    if a.cmd == "status":
        try:
            s = printer.status()
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{a.host}: HTTP {e.code}, check --key/--password")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise SystemExit(f"{a.host}: {e}")
        p, j = s.get("printer", {}), s.get("job") or {}
        print(
            f"{p.get('state')} z={p.get('axis_z')} job={j.get('id')} {j.get('progress')}% "
            f"left={j.get('time_remaining')}s nozzle={p.get('temp_nozzle')} bed={p.get('temp_bed')}"
        )
    elif a.cmd == "render":
        frames = Path(a.frames)
        if not frames.is_dir() and (FRAMES / a.frames).is_dir():
            frames = FRAMES / a.frames
        render(
            frames,
            a.out or RENDER / f"{frames.name}.{'gif' if a.gif else 'mp4'}",
            a.fps or (10 if a.gif else 30),
            a.hold if a.hold is not None else (1 if a.gif else 2),
            a.crf,
            a.max_duration if a.max_duration is not None else (3 if a.gif else 20),
            a.gif,
        )
        if not a.gif:
            poster(
                frames,
                Path(a.out).with_suffix(".jpg")
                if a.out
                else RENDER / f"{frames.name}.jpg",
            )
    elif a.cmd == "capture":
        out = (
            Path(a.out)
            if a.out
            else FRAMES / f"{dt.datetime.now().astimezone():%Y%m%d-%H%M}"
        )
        if a.interval:
            capture_interval(a.camera, out, a.interval, a.duration)
        else:
            capture_layers(printer, a.camera, out, a.poll, a.until_done)
    elif a.cmd == "watch":
        watch(printer, a.camera, a.outdir, a.poll, a.fps, a.max_duration)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
