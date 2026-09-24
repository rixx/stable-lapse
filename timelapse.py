#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["opencv-python-headless", "numpy"]
# ///
"""Render cool 3d print timelapses from an RTSP camera and PrusaLink.

Usage: timelapse.py [command]
    capture  grab frames right now (per layer if PrusaLink is configured, otherwise just per interval)
    watch    wait for print jobs, capture each one per layer, render on finish
    render   build an mp4 for the given directory or for all
    status   one PrusaLink status line

--experimental (capture, watch): reads the camera stream and for each layer keeps
the frame with the printer head (overwrite .head.png if yours differs; matches on
the fan) closest to --target so the timelapse looks cleaner.

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
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import tomllib

CONFIG = (
    Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    / "print-timelapse.toml"
)
DEFAULTS = {
    "camera": {"url": "rtsp://192.168.4.133/live"},
    "printer": {"host": "192.168.4.101", "key": "", "password": "", "user": "maker"},
    "output": {"dir": str(Path(__file__).resolve().parent)},
}
ACTIVE = {"PRINTING", "PAUSED", "ATTENTION"}
Z_STEP = 0.05  # minimum z rise that counts as a new layer
GIF_WIDTH = 640
TEMPLATE = Path(__file__).resolve().parent / ".head.png"
STREAM_FPS = 6
HEAD_SCORE = 0.62  # template match score needed to trust a head position


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%H:%M:%S} {msg}"
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
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-rtsp_transport",
        "tcp",
        "-i",
        url,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(tmp),
    ]
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

    def get(self, path: str, auth: str | None = None) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base + path, headers={"Accept": "application/json"}
        )
        if self.key:
            req.add_header("X-Api-Key", self.key)
        if auth:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r) if r.status != 204 else {}
        except urllib.error.HTTPError as e:
            challenge = e.headers.get("WWW-Authenticate", "")
            if (
                e.code == 401
                and not auth
                and self.password
                and challenge.startswith("Digest")
            ):
                return self.get(path, self._digest("GET", path, challenge))
            raise

    def status(self) -> dict[str, Any]:
        return self.get("/api/v1/status")

    def job(self) -> dict[str, Any]:
        return self.get("/api/v1/job")


class Snap(NamedTuple):
    state: str
    job_id: int | None
    z: float | None
    progress: float | None

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
    return Snap(p.get("state", "?"), j.get("id"), p.get("axis_z"), j.get("progress"))


def job_name(printer: Printer) -> str:
    try:
        return (printer.job().get("file") or {}).get("display_name") or ""
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return ""


def slug(name: str) -> str:
    # Drop technical part (nozzle, etc)
    name = re.split(r"_\d\.\dn_", Path(name).stem, maxsplit=1)[0]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:60] or "print"


class LayerTracker:
    # z must hold for two polls: ramping lift moves z during travels
    def __init__(self) -> None:
        self.layer: float | None = None
        self.prev: float | None = None

    def update(self, z: float | None) -> str | None:
        # "layer" on a new layer, "end" on the end gcode lift, else None
        stable = z is not None and z == self.prev
        self.prev = z
        if (
            not stable
            or z is None
            or (self.layer is not None and z <= self.layer + Z_STEP)
        ):
            return None
        event = (
            "end" if self.layer is not None and z > self.layer + END_LIFT else "layer"
        )
        self.layer = z
        return event


class Session:
    def __init__(self, camera: str, out: Path | str):
        global LOGFILE
        self.camera = camera
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.n = len(list(self.out.glob("[0-9]*.jpg")))
        self.layers = LayerTracker()
        self.last_t = 0.0
        LOGS.mkdir(parents=True, exist_ok=True)
        LOGFILE = LOGS / f"{self.out.name}.log"

    def close(self) -> None:
        global LOGFILE
        LOGFILE = None

    def grab(self, why: str = "") -> bool:
        dest = self.out / f"{self.n:05d}.jpg"
        started = time.monotonic()
        if grab(self.camera, dest):
            self.n += 1
            self.last_t = started
            log(f"{dest.name} {why}")
            return True
        return False

    def maybe_layer(
        self, z: float | None, min_interval: float, max_interval: float
    ) -> None:
        # Grab a new image if z has changed enough or max_interval has passed
        since = time.monotonic() - self.last_t
        if self.layers.update(z):
            if since >= min_interval:
                self.grab(f"z={z:.2f}")
            return
        if max_interval and since >= max_interval:
            self.grab("interval")


class Stream:
    def __init__(self, url: str, fps: float):
        self.url = url
        self.fps = fps
        self.proc: subprocess.Popen[bytes] | None = None
        self.stopped = False

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
                    log("stream ended, restarting")
                    time.sleep(2)
                self.start()
                buf = b""
            assert self.proc is not None and self.proc.stdout is not None
            chunk = self.proc.stdout.read(65536)
            if not chunk:
                continue
            buf += chunk
            while (end := buf.find(b"\xff\xd9")) >= 0:
                start = buf.find(b"\xff\xd8")
                if 0 <= start < end:
                    yield buf[start : end + 2]
                buf = buf[end + 2 :]

    def stop(self) -> None:
        self.stopped = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


class TrackedSession(Session):
    # Experimental: per layer, keep the frame with the print head closest to a target pixel
    def __init__(
        self,
        camera: str,
        out: Path | str,
        template: Path = TEMPLATE,
        target: tuple[int, int] | None = None,
    ):
        super().__init__(camera, out)
        import cv2
        import numpy as np

        self.cv2, self.np = cv2, np
        tpl = cv2.imread(str(template), cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            raise SystemExit(f"cannot read template at {template}")
        self.tpl = cv2.resize(tpl, None, fx=0.5, fy=0.5)
        self.target = target
        self.lock = threading.Lock()
        self.latest: bytes | None = None
        self.best: tuple[float, bytes, tuple[int, int], float] | None = None
        self.stream = Stream(camera, STREAM_FPS)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        for jpg in self.stream.frames():
            pos, score = self._locate(jpg)
            with self.lock:
                self.latest = jpg
                if score < HEAD_SCORE:
                    continue
                if self.target is None:
                    self.target = pos
                    log(f"target {pos} (score {score:.2f})")
                d = math.dist(pos, self.target)
                if self.best is None or d < self.best[0]:
                    self.best = (d, jpg, pos, score)

    def _locate(self, jpg: bytes) -> tuple[tuple[int, int], float]:
        cv2 = self.cv2
        img = cv2.imdecode(self.np.frombuffer(jpg, self.np.uint8), cv2.IMREAD_GRAYSCALE)
        img = cv2.resize(img, None, fx=0.5, fy=0.5)
        _, score, _, (x, y) = cv2.minMaxLoc(
            cv2.matchTemplate(img, self.tpl, cv2.TM_CCOEFF_NORMED)
        )
        h, w = self.tpl.shape
        return ((x + w // 2) * 2, (y + h // 2) * 2), float(score)

    def _write(self, jpg: bytes, why: str) -> bool:
        dest = self.out / f"{self.n:05d}.jpg"
        dest.write_bytes(jpg)
        self.n += 1
        self.last_t = time.monotonic()
        log(f"{dest.name} {why}")
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

    def maybe_layer(
        self, z: float | None, min_interval: float, max_interval: float
    ) -> None:
        if not self.layers.update(z):
            return
        with self.lock:
            best, self.best = self.best, None
            latest = self.latest
        if best:
            d, jpg, pos, score = best
            self._write(jpg, f"z={z:.2f} head={pos} d={d:.0f} score={score:.2f}")
        elif latest:
            self._write(latest, f"z={z:.2f} no head found")

    def close(self) -> None:
        self.stream.stop()


def make_session(camera: str, out: Path | str, a: argparse.Namespace) -> Session:
    if not a.experimental:
        return Session(camera, out)
    target = tuple(int(v) for v in a.target.split(",")) if a.target else None
    assert target is None or len(target) == 2
    return TrackedSession(camera, out, Path(a.template), target)


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
    printer: Printer,
    camera: str,
    out: Path | str,
    poll: float,
    min_interval: float,
    max_interval: float,
    until_done: bool,
    a: argparse.Namespace,
) -> None:
    sess = make_session(camera, out, a)
    log(f"layer capture into {out}")
    sess.grab("start")
    while True:
        snap = snapshot(printer)
        if snap:
            if until_done and snap.state not in ACTIVE:
                sess.grab("end")
                log(f"printer {snap.state}, done: {sess.n} frames")
                sess.close()
                return
            sess.maybe_layer(snap.z, min_interval, max_interval)
        time.sleep(poll)


def watch(
    printer: Printer,
    camera: str,
    outdir: Path | str,
    poll: float,
    min_interval: float,
    max_interval: float,
    fps: int,
    max_duration: int,
    keep_frames: bool,
    a: argparse.Namespace,
) -> None:
    log("watching for jobs")
    job_id: int | None = None
    sess: Session | None = None
    while True:
        snap = snapshot(printer)
        if snap:
            if snap.active and (sess is None or job_id != snap.job_id):
                if sess:
                    finish(sess, fps, max_duration, keep_frames)
                name = slug(job_name(printer))
                if a.experimental:
                    name += "-experimental"
                out = Path(outdir) / f"{dt.datetime.now():%Y%m%d-%H%M}-{name}"
                log(f"job {snap.job_id} {name!r} started")
                job_id, sess = snap.job_id, make_session(camera, out, a)
                sess.grab("start")
            elif sess and not snap.active:
                sess.grab("end")
                log(f"job {job_id} ended ({snap.state})")
                finish(sess, fps, max_duration, keep_frames)
                sess = None
            elif sess:
                sess.maybe_layer(snap.z, min_interval, max_interval)
        time.sleep(poll)


def finish(sess: Session, fps: int, max_duration: int, keep_frames: bool) -> None:
    sess.close()
    if sess.n < 2:
        log(f"{sess.out}: only {sess.n} frames, not rendering")
        return
    try:
        render(
            sess.out, RENDER / f"{sess.out.name}.mp4", fps, max_duration=max_duration
        )
    except subprocess.CalledProcessError as e:
        log(f"render failed: {e.stderr.decode(errors='replace')[-500:]}")
        return
    if not keep_frames:
        shutil.rmtree(sess.out)


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
    crf: int = 20,
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
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-r",
        str(fps),
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat),
        "-filter_complex",
        vf,
        *codec,
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    concat.unlink()
    log(f"{out} ({len(files)} frames, {len(files) / fps + hold:.0f}s)")
    return out


def main() -> None:
    cfg = CFG
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--camera", default=cfg["camera"]["url"])
    ap.add_argument("--host", default=cfg["printer"]["host"])
    ap.add_argument("--key", default=cfg["printer"]["key"], help="PrusaLink API key")
    ap.add_argument(
        "--password", default=cfg["printer"]["password"], help="PrusaLink password"
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    exp = argparse.ArgumentParser(add_help=False)
    exp.add_argument(
        "--experimental",
        action="store_true",
        help="Pick the frame for each layer with the print head closest to --target",
    )
    exp.add_argument(
        "--target",
        help="pixel x,y to park the head at; default: least of the print covered",
    )
    exp.add_argument(
        "--template", default=str(TEMPLATE), help="grayscale crop of the print head"
    )

    c = sub.add_parser("capture", help="Grab frames now", parents=[exp])
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
    c.add_argument(
        "--max-interval",
        type=float,
        default=0,
        help="layer mode: also grab every N s at most",
    )
    c.add_argument("--min-interval", type=float, default=3)
    c.add_argument("--poll", type=float, default=2)

    w = sub.add_parser(
        "watch", help="Run as daemon, create one timelapse per print job", parents=[exp]
    )
    w.add_argument("--outdir", default=str(FRAMES))
    w.add_argument("--fps", type=int, default=30)
    w.add_argument(
        "--max-duration",
        type=int,
        default=20,
        help="seconds; drops frames evenly to fit, 0 = keep all",
    )
    w.add_argument("--max-interval", type=float, default=0)
    w.add_argument("--min-interval", type=float, default=3)
    w.add_argument("--poll", type=float, default=2)
    w.add_argument("--keep-frames", action="store_true")

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
    r.add_argument("--crf", type=int, default=20)
    r.add_argument(
        "--max-duration",
        type=int,
        help="seconds; drops frames evenly to fit, 0 = keep all; default 20, gif 3",
    )

    sub.add_parser("status", help="Print PrusaLink status")

    a = ap.parse_args()
    printer = Printer(a.host, a.key, a.password, cfg["printer"]["user"])

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
    elif a.cmd == "capture":
        out = Path(a.out) if a.out else FRAMES / f"{dt.datetime.now():%Y%m%d-%H%M}"
        if a.interval:
            capture_interval(a.camera, out, a.interval, a.duration)
        else:
            if not (a.key or a.password):
                raise SystemExit("layer mode needs --key or --password (or --interval)")
            capture_layers(
                printer,
                a.camera,
                out,
                a.poll,
                a.min_interval,
                a.max_interval,
                a.until_done,
                a,
            )
    elif a.cmd == "watch":
        if not (a.key or a.password):
            raise SystemExit("watch needs --key or --password")
        watch(
            printer,
            a.camera,
            a.outdir,
            a.poll,
            a.min_interval,
            a.max_interval,
            a.fps,
            a.max_duration,
            a.keep_frames,
            a,
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
