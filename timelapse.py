#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Render cool 3d print timelapses from an RTSP camera and PrusaLink.

Usage: timelapse.py [command]
    capture  grab frames right now (per layer if PrusaLink is configured, otherwise just per interval)
    watch    wait for print jobs, capture each one per layer, render on finish
    render   build an mp4 for the given directory or for all
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
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
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


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%H:%M:%S} {msg}", file=sys.stderr, flush=True)


def load_config() -> dict[str, dict[str, Any]]:
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    if CONFIG.exists():
        for section, values in tomllib.loads(CONFIG.read_text()).items():
            cfg.setdefault(section, {}).update(values)
    return cfg


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
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem).strip("_")[:60] or "print"


class Session:
    def __init__(self, camera: str, out: Path | str):
        self.camera = camera
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.n = len(list(self.out.glob("[0-9]*.jpg")))
        self.last_z: float | None = None
        self.last_t = 0.0

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
        now = time.monotonic()
        since = now - self.last_t
        if z is not None and (self.last_z is None or z > self.last_z + Z_STEP):
            if since >= min_interval and self.grab(f"z={z:.2f}"):
                self.last_z = z
            return
        if max_interval and since >= max_interval:
            self.grab("interval")


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
) -> None:
    sess = Session(camera, out)
    log(f"layer capture into {out}")
    sess.grab("start")
    while True:
        snap = snapshot(printer)
        if snap:
            if until_done and snap.state not in ACTIVE:
                sess.grab("end")
                log(f"printer {snap.state}, done: {sess.n} frames")
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
    keep_frames: bool,
) -> None:
    log("watching for jobs")
    job_id: int | None = None
    sess: Session | None = None
    while True:
        snap = snapshot(printer)
        if snap:
            if snap.active and (sess is None or job_id != snap.job_id):
                if sess:
                    finish(sess, fps, keep_frames)
                name = slug(job_name(printer))
                out = Path(outdir) / f"{dt.datetime.now():%Y%m%d-%H%M}-{name}"
                log(f"job {snap.job_id} {name!r} started")
                job_id, sess = snap.job_id, Session(camera, out)
                sess.grab("start")
            elif sess and not snap.active:
                sess.grab("end")
                log(f"job {job_id} ended ({snap.state})")
                finish(sess, fps, keep_frames)
                sess = None
            elif sess:
                sess.maybe_layer(snap.z, min_interval, max_interval)
        time.sleep(poll)


def finish(sess: Session, fps: int, keep_frames: bool) -> None:
    if sess.n < 2:
        log(f"{sess.out}: only {sess.n} frames, not rendering")
        return
    try:
        render(sess.out, sess.out.with_suffix(".mp4"), fps)
    except subprocess.CalledProcessError as e:
        log(f"render failed: {e.stderr.decode(errors='replace')[-500:]}")
        return
    if not keep_frames:
        shutil.rmtree(sess.out)


def render(
    frames: Path | str, out: Path | str, fps: int = 30, hold: float = 2.0, crf: int = 20
) -> Path:
    frames, out = Path(frames), Path(out)
    files = sorted(frames.glob("*.jpg"))
    if len(files) < 2:
        raise SystemExit(f"{frames}: {len(files)} frames, nothing to render")
    concat = frames / "frames.txt"
    concat.write_text("".join(f"file '{f.name}'\n" for f in files))
    vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
    if hold:
        vf += f",tpad=stop_mode=clone:stop_duration={hold}"
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
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        str(crf),
        "-movflags",
        "+faststart",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    concat.unlink()
    log(f"{out} ({len(files)} frames, {len(files) / fps + hold:.0f}s)")
    return out


def main() -> None:
    cfg = load_config()
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

    c = sub.add_parser("capture", help="Grab frames now")
    c.add_argument("--out", help="frames directory (default: <output dir>/<timestamp>)")
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
        "watch", help="Run as daemon, create one timelapse per print job"
    )
    w.add_argument("--outdir", default=cfg["output"]["dir"])
    w.add_argument("--fps", type=int, default=30)
    w.add_argument("--max-interval", type=float, default=0)
    w.add_argument("--min-interval", type=float, default=3)
    w.add_argument("--poll", type=float, default=2)
    w.add_argument("--keep-frames", action="store_true")

    r = sub.add_parser("render", help="Render an mp4")
    r.add_argument("frames")
    r.add_argument("--out", help="default: <frames dir>.mp4")
    r.add_argument("--fps", type=int, default=30)
    r.add_argument(
        "--hold",
        type=float,
        default=2,
        help="seconds to freeze the last frame for a less abrupt ending",
    )
    r.add_argument("--crf", type=int, default=20)

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
        render(
            a.frames, a.out or Path(a.frames).with_suffix(".mp4"), a.fps, a.hold, a.crf
        )
    elif a.cmd == "capture":
        out = a.out or Path(cfg["output"]["dir"]) / f"{dt.datetime.now():%Y%m%d-%H%M}"
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
            a.keep_frames,
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
