#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Serve timelapses as an index HTML plus detail pages.

Usage: web.py [--host HOST] [--port PORT] [--base DIR]

The detail pages use a random token in the URL, so they can be shared publicly,
with auth or some other security protecting the index page to prevent enumeration.
"""

import argparse
import datetime as dt
import html
import json
import re
import secrets
import struct
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from timelapse import BASE, CFG, META, THUMB, log, read_meta, slug

REPO = "https://github.com/rixx/stable-lapse"
LIVE_WINDOW = 900  # seconds without a new frame after which a print counts as abandoned
SCAN_TTL = 3
PRINTERS = {
    "COREONE": "Prusa CORE One",
    "MK4S": "Prusa MK4S",
    "MK4": "Prusa MK4",
    "MK3.9": "Prusa MK3.9",
    "MK3S": "Prusa MK3S+",
    "MINI": "Prusa MINI+",
    "XL": "Prusa XL",
}


def h(s: Any) -> str:
    return html.escape(str(s), quote=True)


def profile(name: str) -> str:
    # "(vip) Prusament PLA @COREONE HF0.4" -> "Prusament PLA"
    return re.sub(r"^\([^)]*\)\s*|\s*@.*$", "", name).strip()


def duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


def jpeg_size(path: Path) -> tuple[int, int] | None:
    # Walk the JPEG segments to the first SOF marker
    try:
        with path.open("rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            while True:
                marker, length = struct.unpack(">HH", f.read(4))
                if 0xFFC0 <= marker <= 0xFFCF and marker not in (
                    0xFFC4,
                    0xFFC8,
                    0xFFCC,
                ):
                    _, height, width = struct.unpack(">BHH", f.read(5))
                    return width, height
                f.seek(length - 2, 1)
    except (OSError, struct.error):
        return None


def parse_dt(value: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Print:
    dir: Path
    meta: dict[str, Any]
    frames: list[Path]
    video: Path | None
    gif: Path | None
    poster_file: Path

    @property
    def token(self) -> str:
        return str(self.meta["token"])

    @property
    def file_name(self) -> str:
        return (self.meta.get("file") or {}).get("display_name") or ""

    @property
    def title(self) -> str:
        if self.file_name:
            return slug(self.file_name).replace("_", " ")
        parts = self.dir.name.split("-", 2)
        return parts[2].replace("_", " ") if len(parts) > 2 else "Untitled print"

    @property
    def started(self) -> dt.datetime:
        if started := parse_dt(self.meta.get("started")):
            return started
        try:
            stamp = dt.datetime.strptime(self.dir.name[:13], "%Y%m%d-%H%M")  # noqa: DTZ007
            return stamp.astimezone()
        except ValueError:
            return dt.datetime.fromtimestamp(self.dir.stat().st_mtime).astimezone()

    @property
    def ended(self) -> dt.datetime | None:
        return parse_dt(self.meta.get("ended"))

    @property
    def last_activity(self) -> float:
        return self.frames[-1].stat().st_mtime if self.frames else 0

    @property
    def status(self) -> str:
        if self.ended:
            state = self.meta.get("state") or "FINISHED"
            return "done" if state == "FINISHED" else "stopped"
        if self.video:
            return "done"
        if time.time() - self.last_activity < LIVE_WINDOW:
            return "printing"
        return "incomplete"

    @property
    def print_time(self) -> float | None:
        if self.meta.get("joined_late") or not self.ended:
            return self.meta.get("time_printing")
        return (self.ended - self.started).total_seconds()

    @property
    def layers(self) -> int:
        return int(self.meta.get("layers") or len(self.frames))

    @property
    def gcode(self) -> dict[str, str]:
        return self.meta.get("gcode") or {}

    @property
    def poster(self) -> str | None:
        if self.frames or self.video:
            return "poster.jpg"
        if (self.dir / THUMB).exists():
            return "thumb.png"
        return None

    @property
    def latest(self) -> str | None:
        return "latest.jpg" if self.frames else self.poster

    def ensure_poster(self) -> Path | None:
        stale = (
            self.frames
            and self.poster_file.exists()
            and (self.poster_file.stat().st_mtime < self.frames[-1].stat().st_mtime)
        )
        if self.poster_file.exists() and not stale:
            return self.poster_file
        try:
            return poster(self.dir, self.poster_file, self.video)
        except subprocess.CalledProcessError as e:
            log(f"poster failed: {e.stderr.decode(errors='replace')[-300:]}")
            return None

    def facts(self) -> list[tuple[str, str]]:
        g = self.gcode
        out: list[tuple[str, str]] = []
        if model := g.get("printer_model"):
            out.append(("Printer", PRINTERS.get(model, model)))
        if filament := g.get("filament_settings_id") or g.get("filament_type"):
            name = profile(filament)
            colour = g.get("filament_colour", "")
            if re.fullmatch(r"#[0-9A-Fa-f]{6}", colour):
                name = (
                    f'<span class="swatch" style="background:{colour}"></span>{h(name)}'
                )
            else:
                name = h(name)
            out.append(("Filament", name))
        if layer := g.get("layer_height"):
            out.append(("Layer height", h(f"{layer} mm")))
        if nozzle := g.get("nozzle_diameter"):
            out.append(("Nozzle", h(f"{nozzle} mm")))
        if fill := g.get("fill_density"):
            pattern = g.get("fill_pattern", "")
            out.append(("Infill", h(f"{fill} {pattern}".strip())))
        if g.get("support_material") == "1":
            out.append(("Supports", h(g.get("support_material_style", "yes"))))
        if profile_name := g.get("print_settings_id"):
            out.append(("Profile", h(profile(profile_name))))
        if grams := g.get("total filament used [g]") or g.get("filament used [g]"):
            out.append(("Filament used", h(f"{grams} g")))
        out.append(("Layers", h(self.layers)))
        if printed := duration(self.print_time):
            estimate = g.get("estimated printing time (normal mode)")
            out.append(
                ("Print time", h(printed + (f" (est. {estimate})" if estimate else "")))
            )
        elif estimate := g.get("estimated printing time (normal mode)"):
            out.append(("Estimated time", h(estimate)))
        out.append(("Started", h(f"{self.started:%d %b %Y, %H:%M}")))
        return out

    def description(self) -> str:
        g = self.gcode
        bits = []
        if filament := g.get("filament_settings_id") or g.get("filament_type"):
            bits.append(profile(filament))
        if layer := g.get("layer_height"):
            bits.append(f"{layer} mm layers")
        if self.layers:
            bits.append(f"{self.layers} layers")
        if printed := duration(self.print_time):
            bits.append(printed)
        if model := g.get("printer_model"):
            bits.append(PRINTERS.get(model, model))
        bits.append(f"{self.started:%d %b %Y}")
        return " · ".join(bits)

    def video_size(self) -> tuple[int, int] | None:
        if self.frames and (size := jpeg_size(self.frames[-1])):
            return size[0] // 2 * 2, size[1] // 2 * 2
        return None


class Catalog:
    def __init__(self, base: Path):
        self.frames = base / "frames"
        self.render = base / "render"
        self.lock = threading.Lock()
        self.poster_lock = threading.Lock()
        self.scanned = 0.0
        self.prints: list[Print] = []
        self.tokens: dict[str, Print] = {}

    def scan(self) -> None:
        prints = []
        for d in self.frames.iterdir() if self.frames.is_dir() else []:
            if not d.is_dir():
                continue
            meta = read_meta(d)
            if "token" not in meta:
                meta["token"] = secrets.token_urlsafe(9)
                (d / META).write_text(json.dumps(meta, indent=1))
                log(f"{d.name}: new token")
            frames = sorted(d.glob("[0-9]*.jpg"))
            video = self.render / f"{d.name}.mp4"
            gif = self.render / f"{d.name}.gif"
            prints.append(
                Print(
                    d,
                    meta,
                    frames,
                    video if video.exists() else None,
                    gif if gif.exists() else None,
                )
            )
        prints.sort(key=lambda p: p.started, reverse=True)
        self.prints = prints
        self.tokens = {p.token: p for p in prints}

    def fresh(self) -> None:
        with self.lock:
            if time.monotonic() - self.scanned > SCAN_TTL:
                self.scan()
                self.scanned = time.monotonic()

    def all(self) -> list[Print]:
        self.fresh()
        return self.prints

    def get(self, token: str) -> Print | None:
        self.fresh()
        return self.tokens.get(token)


CSS = """
:root {
  --orange: #fa6831; --orange-dark: #e05a25; --bg: #f4f4f2; --card: #fff;
  --text: #1d1d1b; --muted: #6f6f6c; --line: #e4e4e0; --shadow: 0 1px 2px rgba(0,0,0,.06), 0 8px 24px rgba(0,0,0,.06);
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #151515; --card: #202020; --text: #f1f1ef; --muted: #9b9b97; --line: #2e2e2e; --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.35); }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; border-top: 4px solid var(--orange); }
a { color: inherit; text-decoration: none; }
header { max-width: 1100px; margin: 0 auto; padding: 20px 20px 8px; display: flex; align-items: center; gap: 12px; }
header .mark { width: 28px; height: 28px; border-radius: 7px; background: var(--orange); display: grid; place-items: center; flex: none; }
header .mark svg { width: 16px; height: 16px; fill: #fff; }
header h1 { font-size: 20px; margin: 0; font-weight: 700; letter-spacing: -.01em; }
header .sub { color: var(--muted); font-size: 13px; margin-left: auto; }
main { max-width: 1100px; margin: 0 auto; padding: 12px 20px 48px; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 28px 0 12px; font-weight: 600; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 18px; }
.card { background: var(--card); border-radius: 12px; overflow: hidden; box-shadow: var(--shadow); transition: transform .15s, box-shadow .15s; display: flex; flex-direction: column; }
.card:hover { transform: translateY(-2px); box-shadow: 0 2px 4px rgba(0,0,0,.08), 0 14px 32px rgba(0,0,0,.12); }
.poster { aspect-ratio: 16/9; background: #111; position: relative; overflow: hidden; }
.poster img, .poster video { width: 100%; height: 100%; object-fit: cover; display: block; }
.poster.empty { display: grid; place-items: center; color: #666; font-size: 13px; }
.card .body { padding: 12px 14px 14px; }
.card .title { font-weight: 600; font-size: 15px; margin: 0 0 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card .meta { color: var(--muted); font-size: 13px; display: flex; gap: 10px; flex-wrap: wrap; }
.badge { position: absolute; top: 10px; left: 10px; padding: 3px 9px; border-radius: 999px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; background: rgba(0,0,0,.55); color: #fff; backdrop-filter: blur(4px); }
.badge.printing { background: var(--orange); }
.badge.printing::before { content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: #fff; margin-right: 6px; vertical-align: 1px; animation: pulse 1.2s infinite; }
@keyframes pulse { 50% { opacity: .3; } }
.hero { display: grid; grid-template-columns: 3fr 2fr; gap: 0; background: var(--card); border-radius: 14px; overflow: hidden; box-shadow: var(--shadow); }
.hero .poster { aspect-ratio: auto; min-height: 260px; }
.hero .info { padding: 22px 24px; display: flex; flex-direction: column; gap: 10px; }
.hero .info h3 { margin: 0; font-size: 22px; letter-spacing: -.01em; }
.hero .info .file { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
.bar { height: 8px; border-radius: 999px; background: var(--line); overflow: hidden; margin-top: 6px; }
.bar i { display: block; height: 100%; background: var(--orange); border-radius: 999px; }
.stats { display: flex; gap: 22px; flex-wrap: wrap; margin-top: 4px; }
.stats div { font-size: 13px; color: var(--muted); }
.stats b { display: block; font-size: 20px; color: var(--text); font-weight: 600; letter-spacing: -.01em; }
.player { background: #000; border-radius: 14px; overflow: hidden; box-shadow: var(--shadow); }
.player video, .player img { width: 100%; display: block; max-height: 78vh; object-fit: contain; background: #000; }
.detail { display: grid; grid-template-columns: 1fr 300px; gap: 24px; margin-top: 24px; align-items: start; }
.detail h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -.02em; }
.detail .file { color: var(--muted); font-size: 13px; overflow-wrap: anywhere; margin-bottom: 18px; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 8px 18px; margin: 0; font-size: 14px; }
dt { color: var(--muted); }
dd { margin: 0; display: flex; align-items: center; gap: 8px; }
.swatch { width: 14px; height: 14px; border-radius: 4px; border: 1px solid rgba(0,0,0,.15); display: inline-block; flex: none; }
.side { background: var(--card); border-radius: 12px; box-shadow: var(--shadow); overflow: hidden; }
.side img { width: 100%; display: block; background: #fff; }
.side .actions { padding: 12px; display: flex; flex-direction: column; gap: 8px; }
.btn { display: block; text-align: center; padding: 9px 14px; border-radius: 8px; font-weight: 600; font-size: 14px; background: var(--orange); color: #fff; }
.btn:hover { background: var(--orange-dark); }
.btn.ghost { background: transparent; border: 1px solid var(--line); color: var(--text); }
.btn.ghost:hover { border-color: var(--orange); color: var(--orange); }
.empty-state { text-align: center; color: var(--muted); padding: 80px 0; }
footer { max-width: 1100px; margin: 0 auto; padding: 0 20px 24px; color: var(--muted); font-size: 12px; }
footer a:hover { color: var(--orange); }
@media (max-width: 760px) {
  .hero, .detail { grid-template-columns: 1fr; }
  .detail .side { order: 2; }
}
"""

MARK = '<span class="mark"><svg viewBox="0 0 16 16"><path d="M3 2.5v11l10-5.5z"/></svg></span>'


def page(
    title: str,
    body: str,
    sub: str = "",
    brand: str = "Timelapses",
    head: str = "",
) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{h(title)}</title>
{head}
<link rel="icon" href="data:image/svg+xml,{urllib.parse.quote('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" rx="4" fill="#fa6831"/><path d="M5 3.5v9l7-4.5z" fill="#fff"/></svg>')}">
<style>{CSS}</style>
</head>
<body>
<header>{MARK}<h1>{h(brand)}</h1><span class="sub">{sub}</span></header>
<main>
{body}
</main>
<footer><a href="{REPO}">stable-lapse</a></footer>
</body>
</html>
"""


def card(p: Print, lazy: bool) -> str:
    loading = ' loading="lazy"' if lazy else ""
    poster = (
        f'<img src="/p/{p.token}/{p.poster}" alt=""{loading}>'
        if p.poster
        else "no frames"
    )
    status = p.status
    badge = f'<span class="badge {status}">{status}</span>' if status != "done" else ""
    bits = [f"{p.started:%d %b %Y}"]
    if filament := p.gcode.get("filament_type"):
        bits.append(filament)
    if p.layers:
        bits.append(f"{p.layers} layers")
    if printed := duration(p.print_time):
        bits.append(printed)
    return f"""<a class="card" href="/p/{p.token}/">
  <div class="poster{"" if p.poster else " empty"}">{poster}{badge}</div>
  <div class="body"><p class="title" title="{h(p.file_name)}">{h(p.title)}</p>
  <div class="meta">{"".join(f"<span>{h(b)}</span>" for b in bits)}</div></div>
</a>"""


def live_stats(p: Print) -> str:
    progress = float(p.meta.get("progress") or 0)
    remaining = duration(p.meta.get("time_remaining"))
    elapsed = duration(p.meta.get("time_printing"))
    stats = "".join(
        f"<div><b>{h(v)}</b>{h(k)}</div>"
        for k, v in [
            ("done", f"{progress:.0f}%"),
            ("elapsed", elapsed),
            ("remaining", remaining),
            ("layers", p.layers),
        ]
        if v
    )
    return f'<div class="bar"><i style="width:{progress:.1f}%"></i></div><div class="stats">{stats}</div>'


def hero(p: Print) -> str:
    poster = (
        f'<img src="/p/{p.token}/{p.latest}?t={int(time.time())}" alt="">'
        if p.latest
        else ""
    )
    return f"""<a class="hero" href="/p/{p.token}/">
  <div class="poster">{poster}<span class="badge printing">printing</span></div>
  <div class="info"><h3>{h(p.title)}</h3><div class="file">{h(p.file_name)}</div>{live_stats(p)}</div>
</a>"""


def index_page(prints: list[Print]) -> str:
    live = [p for p in prints if p.status == "printing"]
    rest = [p for p in prints if p.status != "printing"]
    body = ""
    if live:
        body += "<h2>Printing now</h2>" + "".join(hero(p) for p in live)
    if rest:
        cards = "".join(card(p, i >= EAGER_IMAGES) for i, p in enumerate(rest))
        body += f"<h2>{len(rest)} prints</h2><div class='grid'>{cards}</div>"
    if not prints:
        body = '<p class="empty-state">No prints yet.</p>'
    return page("Timelapses", body, sub=f"{len(prints)} prints")


def social(p: Print, base: str) -> str:
    url = f"{base}/p/{p.token}/"
    tags = [
        ("og:type", "video.other"),
        ("og:site_name", "stable-lapse"),
        ("og:title", p.title),
        ("og:description", p.description()),
        ("og:url", url),
        ("twitter:card", "summary_large_image"),
        ("twitter:title", p.title),
        ("twitter:description", p.description()),
    ]
    size = p.video_size()
    if p.latest:
        tags += [("og:image", url + p.latest), ("twitter:image", url + p.latest)]
        if p.latest == "latest.jpg" and size:
            tags += [
                ("og:image:width", str(size[0])),
                ("og:image:height", str(size[1])),
            ]
    if p.video and p.status != "printing":
        video = url + "video.mp4"
        tags += [
            ("og:video", video),
            ("og:video:secure_url", video),
            ("og:video:type", "video/mp4"),
        ]
        if size:
            tags += [
                ("og:video:width", str(size[0])),
                ("og:video:height", str(size[1])),
            ]
    return (
        "\n".join(
            f'<meta {"name" if k.startswith("twitter:") else "property"}="{k}" content="{h(v)}">'
            for k, v in tags
        )
        + f'\n<meta name="description" content="{h(p.description())}">'
    )


def scrubber(p: Print) -> str:
    if not p.frames:
        return ""
    names = json.dumps([f.name for f in p.frames])
    last = len(p.frames) - 1
    return f"""<h2>Frame by frame</h2>
<div class="scrub">
  <img src="/p/{p.token}/frames/{p.frames[-1].name}" alt="">
  <div class="bar-row"><input type="range" min="0" max="{last}" value="{last}" aria-label="Frame"><span>frame {last + 1} / {last + 1}</span></div>
</div>
<script>
(() => {{
  const frames = {names};
  const scrub = document.querySelector('.scrub');
  const img = scrub.querySelector('img'), range = scrub.querySelector('input'), label = scrub.querySelector('span');
  range.addEventListener('input', () => {{
    img.src = `/p/{p.token}/frames/${{frames[range.value]}}`;
    label.textContent = `frame ${{+range.value + 1}} / ${{frames.length}}`;
  }});
}})();
</script>"""


def detail_page(p: Print, base: str) -> str:
    live = p.status == "printing"
    if p.video and not live:
        player = (
            f'<video controls autoplay muted loop playsinline preload="auto" poster="/p/{p.token}/poster.jpg">'
            f'<source src="/p/{p.token}/video.mp4" type="video/mp4"></video>'
            "<script>document.querySelector('video').play().catch(() => {});</script>"
        )
    elif p.latest:
        player = f'<img src="/p/{p.token}/{p.latest}?t={int(time.time())}" alt="">'
    else:
        player = '<div class="poster empty">no frames yet</div>'
    facts = "".join(f"<dt>{h(k)}</dt><dd>{v}</dd>" for k, v in p.facts())
    status = p.status
    if status != "done":
        facts = (
            f'<dt>Status</dt><dd><span class="badge {status}" style="position:static">{status}</span></dd>'
            + facts
        )
    actions = ""
    if p.video:
        actions += f'<a class="btn" href="/p/{p.token}/video.mp4" download="{h(p.dir.name)}.mp4">Download mp4</a>'
    if p.gif:
        actions += f'<a class="btn ghost" href="/p/{p.token}/video.gif" download="{h(p.dir.name)}.gif">Download gif</a>'
    thumb = (
        f'<img src="/p/{p.token}/thumb.png" alt="Slicer preview">'
        if (p.dir / THUMB).exists()
        else ""
    )
    side = (
        f'<div class="side">{thumb}<div class="actions">{actions}</div></div>'
        if thumb or actions
        else ""
    )
    body = f"""<div class="player">{player}</div>
<div class="detail">
  <div><h1>{h(p.title)}</h1><div class="file">{h(p.file_name)}</div>{live_stats(p) if live else ""}<dl style="margin-top:{"18px" if live else "0"}">{facts}</dl></div>
  {side}
</div>
{scrubber(p)}"""
    return page(
        p.title,
        body,
        sub=f"{p.started:%d %b %Y}",
        brand="Timelapse",
        head=social(p, base),
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "stable-lapse"
    protocol_version = "HTTP/1.1"
    catalog: Catalog

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_HEAD(self) -> None:
        self.route(head=True)

    def do_GET(self) -> None:
        self.route()

    def route(self, head: bool = False) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            return self.html(index_page(self.catalog.all()), head)
        m = re.fullmatch(r"/p/([A-Za-z0-9_-]{8,})(/.*)?", path)
        p = self.catalog.get(m[1]) if m else None
        if not m or not p:
            return self.fail(HTTPStatus.NOT_FOUND)
        rest = m[2] or ""
        done = p.status in ("done", "stopped")
        if rest == "":
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", path + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if rest == "/":
            return self.html(detail_page(p, self.base_url()), head)
        if rest == "/video.mp4" and p.video:
            return self.file(p.video, "video/mp4", head, done)
        if rest == "/video.gif" and p.gif:
            return self.file(p.gif, "image/gif", head, done)
        if rest == "/poster.jpg" and p.poster == "poster.jpg":
            with self.catalog.poster_lock:
                poster_file = p.ensure_poster()
            if poster_file:
                return self.file(poster_file, "image/jpeg", head, done)
            return self.fail(HTTPStatus.NOT_FOUND)
        if rest == "/thumb.png" and (p.dir / THUMB).exists():
            return self.file(p.dir / THUMB, "image/png", head, done)
        if rest == "/latest.jpg" and p.frames:
            return self.file(p.frames[-1], "image/jpeg", head, done)
        if rest == "/first.jpg" and p.frames:
            return self.file(p.frames[0], "image/jpeg", head, done)
        if rest == "/meta.json":
            return self.html(json.dumps(p.meta, indent=1), head, "application/json")
        if fm := re.fullmatch(r"/frames/(\d{5})\.jpg", rest):
            frame = p.dir / f"{fm[1]}.jpg"
            if frame.exists():
                return self.file(frame, "image/jpeg", head, done)
        return self.fail(HTTPStatus.NOT_FOUND)

    def base_url(self) -> str:
        if url := CFG["web"].get("url"):
            return str(url).rstrip("/")
        proto = self.headers.get("X-Forwarded-Proto", "http")
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
        return f"{proto}://{host}"

    def fail(self, status: HTTPStatus) -> None:
        body = f"{status.value} {status.phrase}\n".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def html(
        self, text: str, head: bool, ctype: str = "text/html; charset=utf-8"
    ) -> None:
        body = text.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def file(self, path: Path, ctype: str, head: bool, immutable: bool) -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            first, _, last = rng[6:].partition("-")
            if first:
                start, end = int(first), int(last) if last else size - 1
            elif last:
                start = max(0, size - int(last))
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header(
            "Cache-Control", "public, max-age=86400" if immutable else "no-cache"
        )
        self.send_header(
            "Last-Modified", self.date_time_string(int(path.stat().st_mtime))
        )
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        with path.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(left, 1 << 16))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--host", default=CFG["web"]["host"])
    ap.add_argument("--port", type=int, default=CFG["web"]["port"])
    ap.add_argument(
        "--base", default=str(BASE), help="directory with frames/ and render/"
    )
    a = ap.parse_args()
    Handler.catalog = Catalog(Path(a.base))
    server = ThreadingHTTPServer((a.host, a.port), Handler)
    server.daemon_threads = True
    log(f"serving {a.base} on http://{a.host}:{a.port}/")
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
