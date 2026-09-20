#!/usr/bin/env python3
"""Self-hosted GitHub profile cards — zero third-party services.

Renders local SVGs from public GitHub data:

    assets/stats.svg     — stars · contributions (last year) · followers
    assets/streak.svg    — total contributions · current streak · longest streak
    assets/top-langs.svg — most used languages (donut + legend)
    assets/snake.svg     — snake game over the real contribution grid

Data sources: the GitHub GraphQL/REST APIs (with the standard
``GITHUB_TOKEN`` available in Actions) and, as a token-free fallback, the
public ``github.com/users/<user>/contributions`` page. Only the Python
standard library is used so the workflow needs no ``pip install`` step.

Usage:
    python3 tools/generate.py [--demo] [--user notolac] [--out assets]

``--demo`` renders the cards with synthetic data (layout preview only).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from collections import deque

API = "https://api.github.com"
GQL = "https://api.github.com/graphql"

# ---------------------------------------------------------------- theme ---
# "radical"-style palette: dark violet background, pink accents, yellow
# highlight, cyan secondary text.

BG = "#141321"
BORDER = "#e4e2e2"
PINK = "#fe428e"
YELLOW = "#f8d847"
CYAN = "#a9fef7"
MUTED = "#8b88a8"
SNAKE = "#a371f7"

# GitHub dark-mode contribution scale (index = level 0..4). Level 0 is
# lifted a little so empty cells stay visible on the violet background.
SCALE = ["#2a2941", "#0e4429", "#006d32", "#26a641", "#39d353"]

LEVELS = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2,
          "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}

LANG_COLORS = {
    "Python": "#3572A5",
    "Shell": "#89e051",
    "JavaScript": "#f1e05a",
    "TypeScript": "#3178c6",
    "HTML": "#e34c26",
    "CSS": "#563d7c",
    "SCSS": "#c6538c",
    "Vue": "#41b883",
    "Dockerfile": "#384d54",
    "HCL": "#844FBA",
    "PowerShell": "#012456",
    "Batchfile": "#C1F12E",
    "Go": "#00ADD8",
    "Rust": "#dea584",
    "Java": "#b07219",
    "Kotlin": "#A97BFF",
    "Ruby": "#701516",
    "PHP": "#4F5D95",
    "C": "#555555",
    "C++": "#f34b7d",
    "C#": "#178600",
    "Swift": "#F05138",
    "Lua": "#000080",
    "Perl": "#0298c3",
    "R": "#198CE7",
    "Jupyter Notebook": "#DA5B0B",
    "Markdown": "#083fa1",
    "Makefile": "#427819",
    "YAML": "#cb171e",
    "Astro": "#ff5a03",
}
FALLBACK_LANG_COLORS = ["#fe428e", "#f8d847", "#a9fef7", "#a371f7",
                        "#f78166", "#39d353"]

FONT = ("font-family=\"'Segoe UI',Ubuntu,-apple-system,BlinkMacSystemFont,"
        "Helvetica,Arial,sans-serif\"")

# Material "whatshot" flame, 24x24 viewbox.
FLAME = ("M13.5.67s.74 2.65.74 4.8c0 2.06-1.35 3.73-3.41 3.73-2.07 0-3.63-1.67"
         "-3.63-3.73l.03-.36C5.21 7.51 4 10.62 4 14c0 4.42 3.58 8 8 8s8-3.58 8-8"
         "C20 8.61 17.41 3.8 13.5.67zM11.71 19c-1.78 0-3.22-1.4-3.22-3.14 0-1.62"
         " 1.05-2.76 2.81-3.12 1.77-.36 3.6-1.21 4.62-2.58.39 1.29.59 2.65.59 "
         "4.04 0 2.65-2.15 4.8-4.8 4.8z")


def esc(s: object) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def fmt_num(n: int) -> str:
    return f"{n:,}"


def fmt_date(d: dt.date, with_year: bool = True) -> str:
    s = f"{d:%b} {d.day}"
    return f"{s}, {d.year}" if with_year else s


def fmt_range(a: dt.date | None, b: dt.date | None) -> str:
    if a is None or b is None:
        return ""
    year = dt.date.today().year
    if a == b:
        return fmt_date(a, a.year != year)
    if a.year == b.year == year:
        return f"{fmt_date(a, False)} - {fmt_date(b, False)}"
    return f"{fmt_date(a)} - {fmt_date(b)}"


# ----------------------------------------------------------------- http ---

def _opener() -> urllib.request.OpenerDirector:
    # Direct connection: some sandboxes / corporate proxies inject
    # credentials that api.github.com rejects with 401.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


_OPENER = _opener()


def _req(url: str, token: str | None, method: str = "GET",
         payload: dict | None = None) -> object:
    data = json.dumps(payload).encode() if payload is not None else None

    def build(tok: str | None) -> urllib.request.Request:
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "notolac-profile-stats/2.0")
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
        return req

    try:
        with _OPENER.open(build(token), timeout=30) as res:
            return json.load(res)
    except urllib.error.HTTPError as exc:
        # A stale/expired token must not break public data: retry
        # anonymously for simple GETs (GraphQL still needs auth).
        if exc.code == 401 and token and method == "GET":
            print("info: token rejected, retrying without auth",
                  file=sys.stderr)
            with _OPENER.open(build(None), timeout=30) as res:
                return json.load(res)
        raise


def _html(url: str) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "notolac-profile-stats/2.0"})
    with _OPENER.open(req, timeout=30) as res:
        return res.read().decode("utf-8", "replace")


# -------------------------------------------------------------- fetching ---

def fetch_user(token: str | None, user: str) -> dict:
    try:
        return _req(f"{API}/users/{user}", token)  # type: ignore[return-value]
    except Exception as exc:  # offline / rate-limited: degrade gracefully
        print(f"warn: user fetch failed ({exc})", file=sys.stderr)
        return {}


def fetch_repos(token: str | None, user: str) -> list[dict]:
    repos: list[dict] = []
    page = 1
    try:
        while True:
            batch = _req(
                f"{API}/users/{user}/repos?per_page=100&page={page}&type=owner",
                token,
            )
            assert isinstance(batch, list)
            repos.extend(batch)
            if len(batch) < 100:
                break
            page += 1
    except Exception as exc:
        print(f"warn: repos fetch failed ({exc})", file=sys.stderr)
    return repos


def fetch_languages(token: str | None, repos: list[dict]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for repo in repos:
        if repo.get("fork"):
            continue
        url = repo.get("languages_url")
        if not url:
            continue
        try:
            langs = _req(url, token)
            assert isinstance(langs, dict)
            for lang, nbytes in langs.items():
                totals[lang] = totals.get(lang, 0) + int(nbytes)
        except Exception as exc:
            print(f"warn: languages fetch failed for {repo.get('name')} ({exc})",
                  file=sys.stderr)
    return totals


# A "day" is {"date": "YYYY-MM-DD", "count": int, "level": 0..4}.
# ``window`` = the rolling last-year grid exactly as GitHub shows it on the
# profile (same cells, same quartile levels). ``history`` = date -> count
# for the whole account lifetime (used for all-time totals and streaks).

def _gql_days(cal: dict) -> list[dict]:
    return [{"date": d["date"], "count": int(d["contributionCount"]),
             "level": LEVELS.get(d.get("contributionLevel", ""), 0)}
            for w in cal["weeks"] for d in w["contributionDays"]]


def _calendar_from_gql(token: str, user: str) -> tuple[list[dict], dict[str, int]]:
    query = """
    query ($login: String!) {
      user(login: $login) {
        contributionsCollection {
          contributionYears
          contributionCalendar {
            weeks { contributionDays { date contributionCount contributionLevel } }
          }
        }
      }
    }"""
    res = _req(GQL, token, method="POST",
               payload={"query": query, "variables": {"login": user}})
    assert isinstance(res, dict) and "data" in res, res
    coll = res["data"]["user"]["contributionsCollection"]
    window = _gql_days(coll["contributionCalendar"])
    history = {d["date"]: d["count"] for d in window}

    years = sorted(int(y) for y in coll.get("contributionYears", []))
    if years:
        parts = [
            f'y{y}: contributionsCollection(from: "{y}-01-01T00:00:00Z", '
            f'to: "{y}-12-31T23:59:59Z") {{ contributionCalendar {{ weeks {{ '
            f'contributionDays {{ date contributionCount }} }} }} }}'
            for y in years]
        q = ("query ($login: String!) { user(login: $login) { "
             + " ".join(parts) + " } }")
        res = _req(GQL, token, method="POST",
                   payload={"query": q, "variables": {"login": user}})
        assert isinstance(res, dict) and "data" in res, res
        for y in years:
            cal = res["data"]["user"][f"y{y}"]["contributionCalendar"]
            for w in cal["weeks"]:
                for d in w["contributionDays"]:
                    history.setdefault(d["date"], int(d["contributionCount"]))
    return window, history


def _parse_contrib_html(html: str) -> list[dict]:
    """Parse cells of the public contributions page.

    Cells carry ``data-date``/``data-level``; the exact count lives in the
    associated ``tool-tip`` (``for`` attribute -> cell ``id``).
    """
    days: list[dict] = []
    for m in re.finditer(r"<td\b[^>]*>", html):
        tag = m.group(0)
        date = re.search(r'data-date="(\d{4}-\d{2}-\d{2})"', tag)
        if not date:
            continue
        lvl = re.search(r'data-level="(\d)"', tag)
        cid = re.search(r'id="([^"]+)"', tag)
        days.append({"date": date.group(1),
                     "level": int(lvl.group(1)) if lvl else 0,
                     "id": cid.group(1) if cid else ""})
    tips = dict(re.findall(
        r'<tool-tip[^>]*\bfor="([^"]+)"[^>]*>([^<]*)</tool-tip>', html))
    for d in days:
        tip = tips.get(d.pop("id"), "")
        m = re.match(r"\s*(\d[\d,]*) contribution", tip)
        d["count"] = int(m.group(1).replace(",", "")) if m else 0
    if not days:
        raise RuntimeError("no contribution cells found in HTML")
    return days


def _calendar_from_html(user: str, created: dt.date | None
                        ) -> tuple[list[dict], dict[str, int]]:
    base = f"https://github.com/users/{user}/contributions"
    window = _parse_contrib_html(_html(base))
    history = {d["date"]: d["count"] for d in window}

    today = dt.date.today()
    first_year = created.year if created else 2008
    for y in range(first_year, today.year + 1):
        try:
            days = _parse_contrib_html(
                _html(f"{base}?from={y}-01-01&to={y}-12-31"))
        except Exception as exc:
            print(f"warn: year {y} fetch failed ({exc})", file=sys.stderr)
            continue
        for d in days:
            if d["date"].startswith(str(y)):
                history.setdefault(d["date"], d["count"])
    return window, history


def fetch_calendar(token: str | None, user: str, created: dt.date | None
                   ) -> tuple[list[dict], dict[str, int]]:
    if token:
        try:
            return _calendar_from_gql(token, user)
        except Exception as exc:
            print(f"warn: calendar via GraphQL failed ({exc}), "
                  "falling back to public HTML", file=sys.stderr)
    try:
        return _calendar_from_html(user, created)
    except Exception as exc:
        print(f"warn: calendar fetch failed ({exc})", file=sys.stderr)
        return [], {}


def demo_calendar() -> tuple[list[dict], dict[str, int]]:
    """Synthetic account lifetime for layout previews (``--demo``)."""
    import random
    random.seed(7)
    today = dt.date.today()
    day = dt.date(2023, 1, 12)
    history: dict[str, int] = {}
    while day <= today:
        history[day.isoformat()] = random.choices(
            [0, 0, 0, 1, 2, 3, 5, 9], weights=[30, 20, 10, 15, 10, 7, 5, 3])[0]
        day += dt.timedelta(days=1)
    for i in range(3):  # keep a live streak so the ring shows something
        history[(today - dt.timedelta(days=i)).isoformat()] = 2 + i
    start = today - dt.timedelta(days=364)
    window = [{"date": d, "count": c, "level": level_by_count(c)}
              for d, c in sorted(history.items()) if d >= start.isoformat()]
    return window, history


def level_by_count(count: int) -> int:
    if count == 0:
        return 0
    if count == 1:
        return 1
    if count <= 3:
        return 2
    if count <= 5:
        return 3
    return 4


# --------------------------------------------------------------- streaks ---

class Streaks:
    total = 0
    first: dt.date | None = None
    current = 0
    current_range: tuple[dt.date | None, dt.date | None] = (None, None)
    longest = 0
    longest_range: tuple[dt.date | None, dt.date | None] = (None, None)


def streaks(history: dict[str, int]) -> Streaks:
    s = Streaks()
    today = dt.date.today()
    days = sorted((dt.date.fromisoformat(k), v) for k, v in history.items()
                  if k <= today.isoformat())
    if not days:
        s.current_range = (today, today)
        return s
    s.total = sum(v for _, v in days)
    s.first = next((d for d, v in days if v > 0), None)

    run_start: dt.date | None = None
    prev: dt.date | None = None
    for d, v in days:
        if v > 0:
            if run_start is None or prev is None or (d - prev).days != 1:
                run_start = d
            run = (d - run_start).days + 1
            if run > s.longest:
                s.longest, s.longest_range = run, (run_start, d)
            prev = d
        else:
            run_start, prev = None, None

    counts = dict(days)
    end = today if counts.get(today, 0) > 0 else today - dt.timedelta(days=1)
    cur = 0
    d = end
    while counts.get(d, 0) > 0:
        cur += 1
        d -= dt.timedelta(days=1)
    s.current = cur
    s.current_range = (d + dt.timedelta(days=1), end) if cur else (today, today)
    return s


# ------------------------------------------------------------------ svg ---

def card_open(w: int, h: int, uid: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" {FONT} role="img" aria-labelledby="t-{uid}">',
        f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="6" '
        f'fill="{BG}" stroke="{BORDER}" stroke-opacity="0.9"/>',
    ]


def stat_column(x: float, top: float, value: str, label: str, sub: str,
                color: str, uid: str, delay: float) -> list[str]:
    """Big number + colored label + muted sub-label (fade-in animated)."""
    return [
        f'<g opacity="0"><animate attributeName="opacity" from="0" to="1" '
        f'dur="0.6s" begin="{delay:.1f}s" fill="freeze"/>',
        f'<text x="{x}" y="{top + 38}" font-size="28" font-weight="700" '
        f'fill="{color}" text-anchor="middle">{esc(value)}</text>',
        f'<text x="{x}" y="{top + 66}" font-size="13" font-weight="600" '
        f'fill="{color}" text-anchor="middle">{esc(label)}</text>',
        f'<text x="{x}" y="{top + 90}" font-size="11" fill="{CYAN}" '
        f'text-anchor="middle">{esc(sub)}</text>',
        "</g>",
    ]


def ring_column(x: float, cy: float, value: str, label: str, sub: str,
                ring: str, color: str, uid: str, icon: bool) -> list[str]:
    """Number inside an animated ring, optional flame on top."""
    r = 38
    gap = 26 if icon else 0  # half-angle (deg) of the opening at the top
    a1 = math.radians(-90 + gap)
    a2 = math.radians(-90 - gap)
    p1 = (x + r * math.cos(a1), cy + r * math.sin(a1))
    p2 = (x + r * math.cos(a2), cy + r * math.sin(a2))
    if icon:
        arc = (f'M {p1[0]:.2f} {p1[1]:.2f} A {r} {r} 0 1 1 '
               f'{p2[0]:.2f} {p2[1]:.2f}')
    else:
        arc = (f'M {x} {cy - r} A {r} {r} 0 1 1 {x} {cy + r} '
               f'A {r} {r} 0 1 1 {x} {cy - r}')
    length = 2 * math.pi * r * (360 - 2 * gap) / 360
    p = [
        f'<path d="{arc}" fill="none" stroke="{ring}" stroke-width="5" '
        f'stroke-linecap="round" stroke-dasharray="{length:.1f}" '
        f'stroke-dashoffset="{length:.1f}">'
        f'<animate attributeName="stroke-dashoffset" from="{length:.1f}" '
        f'to="0" dur="1.2s" begin="0.2s" fill="freeze"/></path>',
    ]
    if icon:
        s = 1.05
        p.append(f'<path d="{FLAME}" fill="{ring}" transform="translate('
                 f'{x - 12 * s:.2f} {cy - r - 12 * s:.2f}) scale({s})"/>')
    p += [
        f'<g opacity="0"><animate attributeName="opacity" from="0" to="1" '
        f'dur="0.6s" begin="0.6s" fill="freeze"/>',
        f'<text x="{x}" y="{cy + 9}" font-size="26" font-weight="700" '
        f'fill="{color}" text-anchor="middle">{esc(value)}</text>',
        f'<text x="{x}" y="{cy + r + 22}" font-size="13" font-weight="700" '
        f'fill="{color}" text-anchor="middle">{esc(label)}</text>',
        f'<text x="{x}" y="{cy + r + 42}" font-size="11" fill="{CYAN}" '
        f'text-anchor="middle">{esc(sub)}</text>',
        "</g>",
    ]
    return p


def three_col_card(uid: str, title: str, left: tuple, center: tuple,
                   right: tuple, icon: bool = True) -> str:
    """The layout of the example card: two stat columns flanking a ring.

    ``left``/``right`` = (value, label, sub); ``center`` = (value, label,
    sub, number_color).
    """
    w, h = 440, 190
    p = card_open(w, h, uid)
    p.append(f'<title id="t-{uid}">{esc(title)}</title>')
    xs = (w / 6, w / 2, 5 * w / 6)
    for x in (w / 3, 2 * w / 3):
        p.append(f'<line x1="{x:.1f}" y1="30" x2="{x:.1f}" y2="{h - 30}" '
                 f'stroke="{BORDER}" stroke-opacity="0.35"/>')
    top = 46
    p += stat_column(xs[0], top, *left, PINK, uid, 0.1)
    p += ring_column(xs[1], 74, *center[:3], PINK, center[3], uid, icon)
    p += stat_column(xs[2], top, *right, PINK, uid, 0.3)
    p.append("</svg>")
    return "\n".join(p)


# -------------------------------------------------------------- renderers ---

def render_streak(s: Streaks) -> str:
    since = f"{fmt_date(s.first)} - Present" if s.first else "—"
    return three_col_card(
        "streak", "Contribution streak",
        (fmt_num(s.total), "Total Contributions", since),
        (fmt_num(s.current), "Current Streak", fmt_range(*s.current_range),
         YELLOW),
        (fmt_num(s.longest), "Longest Streak", fmt_range(*s.longest_range)),
    )


def render_stats(user: dict, stars: int, repos: list[dict],
                 last_year: int) -> str:
    public = user.get("public_repos", len(repos))
    return three_col_card(
        "stats", "GitHub stats",
        (fmt_num(stars), "Total Stars", f"across {public} public repos"),
        (fmt_num(last_year), "Contributions", "last 12 months", YELLOW),
        (fmt_num(int(user.get("followers", 0) or 0)), "Followers",
         f"following {int(user.get('following', 0) or 0)}"),
        icon=False,
    )


def render_top_langs(totals: dict[str, int]) -> str:
    w, h = 440, 190
    p = card_open(w, h, "langs")
    p.append('<title id="t-langs">Most used languages</title>')
    p.append(f'<text x="24" y="34" font-size="15" font-weight="700" '
             f'fill="{PINK}">Most Used Languages</text>')
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    grand = sum(totals.values()) or 1
    top = ranked[:6]
    if len(ranked) > 6:  # fold the tail into a single "Other" row
        top = ranked[:5] + [("Other", sum(n for _, n in ranked[5:]))]
    if not top:
        p.append(f'<text x="24" y="100" font-size="12" fill="{CYAN}">'
                 "no language data yet</text>")
        p.append("</svg>")
        return "\n".join(p)

    cx, cy, r, sw = 96, 112, 46, 16
    circ = 2 * math.pi * r
    offset = 0.0
    p.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
             f'stroke="{SCALE[0]}" stroke-width="{sw}"/>')
    for i, (lang, nbytes) in enumerate(top):
        color = LANG_COLORS.get(lang) or FALLBACK_LANG_COLORS[i % 6]
        seg = circ * nbytes / grand
        p.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
            f'stroke-width="{sw}" stroke-dasharray="0 {circ:.2f}" '
            f'stroke-dashoffset="{-offset:.2f}" '
            f'transform="rotate(-90 {cx} {cy})">'
            f'<animate attributeName="stroke-dasharray" '
            f'from="0 {circ:.2f}" to="{max(seg - 1.5, 0):.2f} {circ:.2f}" '
            f'dur="1s" begin="{0.15 + i * 0.1:.2f}s" fill="freeze"/></circle>')
        offset += seg
    p.append(f'<text x="{cx}" y="{cy + 5}" font-size="14" font-weight="700" '
             f'fill="{CYAN}" text-anchor="middle">{len(totals)}</text>')
    p.append(f'<text x="{cx}" y="{cy + 19}" font-size="9" fill="{MUTED}" '
             f'text-anchor="middle">languages</text>')

    x0, y = 186, 62
    for i, (lang, nbytes) in enumerate(top):
        color = LANG_COLORS.get(lang) or FALLBACK_LANG_COLORS[i % 6]
        pct = 100 * nbytes / grand
        p.append(f'<g opacity="0"><animate attributeName="opacity" from="0" '
                 f'to="1" dur="0.5s" begin="{0.2 + i * 0.1:.1f}s" '
                 f'fill="freeze"/>')
        p.append(f'<circle cx="{x0}" cy="{y - 4}" r="5" fill="{color}"/>')
        p.append(f'<text x="{x0 + 14}" y="{y}" font-size="12" '
                 f'font-weight="600" fill="{CYAN}">{esc(lang)}</text>')
        p.append(f'<text x="{w - 24}" y="{y}" font-size="12" '
                 f'font-weight="700" fill="{PINK}" text-anchor="end">'
                 f'{pct:.1f}%</text>')
        p.append("</g>")
        y += 22
    p.append("</svg>")
    return "\n".join(p)


# ---------------------------------------------------------------- snake ---

Cell = tuple[int, int]  # (column, row)


def grid_from_window(window: list[dict]) -> tuple[int, dict[Cell, dict]]:
    """Map the rolling-year days to (column, row) exactly like GitHub:
    Sunday-first rows, one column per week, first column may be partial."""
    if not window:
        return 53, {}
    days = sorted(window, key=lambda d: d["date"])
    first = dt.date.fromisoformat(days[0]["date"])
    sunday = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    cells: dict[Cell, dict] = {}
    for d in days:
        date = dt.date.fromisoformat(d["date"])
        col = (date - sunday).days // 7
        row = (date.weekday() + 1) % 7
        cells[(col, row)] = d
    cols = max(c for c, _ in cells) + 1
    return cols, cells


def simulate_snake(cols: int, rows: int, targets: set[Cell], length: int,
                   pause: int = 12) -> tuple[list[Cell], dict[Cell, int]]:
    """Play the game: a snake of ``length`` cells enters from the left, eats
    every contribution cell (nearest first, never crossing itself) and
    leaves the grid where it came in, so the loop restarts seamlessly.
    ``pause`` extra off-grid steps give the empty board a moment on screen
    before everything resets.

    Returns the head trajectory and, for every eaten cell, the step index at
    which the head reached it.
    """
    home: Cell = (-1, 0)
    body: deque[Cell] = deque((-1 - i, 0) for i in range(length))  # head first
    heads: list[Cell] = [home]
    eaten: dict[Cell, int] = {}
    remaining = set(targets)

    def inside(c: Cell, outro: bool) -> bool:
        col, row = c
        if 0 <= col < cols and 0 <= row < rows:
            return True
        return outro and row == 0 and -length - 1 - pause <= col < 0

    def neighbours(c: Cell) -> list[Cell]:
        col, row = c
        return [(col + 1, row), (col, row + 1), (col - 1, row), (col, row - 1)]

    def bfs(goal_test, outro: bool) -> list[Cell] | None:
        """Shortest path from the head to the closest cell satisfying
        ``goal_test``; the body (except the tail, which moves away) blocks."""
        blocked = set(list(body)[:-1])
        start = body[0]
        prev: dict[Cell, Cell | None] = {start: None}
        q: deque[Cell] = deque([start])
        while q:
            cur = q.popleft()
            if cur != start and goal_test(cur):
                path = []
                while cur != start:
                    path.append(cur)
                    cur = prev[cur]  # type: ignore[assignment]
                return path[::-1]
            for n in neighbours(cur):
                if n in prev or n in blocked or not inside(n, outro):
                    continue
                prev[n] = cur
                q.append(n)
        return None

    def advance(cell: Cell) -> None:
        body.appendleft(cell)
        body.pop()
        heads.append(cell)
        if cell in remaining:
            remaining.discard(cell)
            eaten[cell] = len(heads) - 1

    def wander(outro: bool) -> None:
        blocked = set(list(body)[:-1])
        for n in neighbours(body[0]):
            if n not in blocked and inside(n, outro):
                advance(n)
                return
        advance(body[-1])  # boxed in: follow the tail (always legal)

    guard = 0
    while remaining and guard < 20000:
        guard += 1
        path = bfs(lambda c: c in remaining, False)
        if path is None:
            wander(False)
            continue
        for cell in path:
            advance(cell)

    # outro: back to the entrance, then fully off-grid to the left
    path = bfs(lambda c: c == home, True)
    if path is None:
        wander(True)
        path = bfs(lambda c: c == home, True) or []
    for cell in path:
        advance(cell)
    col = body[0][0]
    while col > -length - 1 - pause:  # off-grid = a short breather
        col -= 1
        advance((col, 0))
    return heads, eaten


def render_snake(window: list[dict]) -> str:
    cell, gap = 13, 3
    step = cell + gap
    pad = 14
    rows = 7
    cols, cells = grid_from_window(window)
    cols = max(cols, 53)
    gw, gh = cols * step - gap, rows * step - gap
    w, h = pad * 2 + gw, pad * 2 + gh

    targets = {c for c, d in cells.items() if d["count"] > 0}
    length = 5
    heads, eaten = simulate_snake(cols, rows, targets, length)
    steps = len(heads) - 1
    dt_step = 0.1
    dur = steps * dt_step

    def xy(c: Cell) -> tuple[int, int]:
        return pad + c[0] * step, pad + c[1] * step

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
         f'viewBox="0 0 {w} {h}" {FONT} role="img" aria-labelledby="t-snake">',
         '<title id="t-snake">Snake eating my last-year GitHub '
         'contributions</title>',
         "<defs>",
         f'<clipPath id="grid"><rect x="{pad - 2}" y="{pad - 2}" '
         f'width="{gw + 4}" height="{gh + 4}" rx="3"/></clipPath>',
         "</defs>",
         f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="6" '
         f'fill="{BG}" stroke="{BORDER}" stroke-opacity="0.9"/>']

    # board: every cell, coloured with GitHub's own level; contribution
    # cells switch to "empty" the moment the head reaches them and come
    # back when the loop restarts.
    for col in range(cols):
        for row in range(rows):
            x, y = xy((col, row))
            d = cells.get((col, row))
            if d is None:  # future days / partial first week
                p.append(f'<rect x="{x}" y="{y}" width="{cell}" '
                         f'height="{cell}" rx="3" fill="{SCALE[0]}"/>')
                continue
            color = SCALE[d.get("level", level_by_count(d["count"]))]
            if d["count"] == 0:
                color = SCALE[0]
            anim = ""
            if (col, row) in eaten and steps:
                t = eaten[(col, row)] / steps
                anim = (f'<animate attributeName="fill" calcMode="discrete" '
                        f'values="{color};{SCALE[0]};{SCALE[0]}" '
                        f'keyTimes="0;{t:.5f};1" dur="{dur:.1f}s" '
                        f'repeatCount="indefinite"/>')
            p.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" '
                     f'rx="3" fill="{color}"><title>{d["date"]}: '
                     f'{d["count"]} contribution'
                     f'{"s" if d["count"] != 1 else ""}</title>{anim}</rect>')

    # snake: each segment replays the head trajectory with a k-step lag.
    init = [(-1 - i, 0) for i in range(length)]
    p.append('<g clip-path="url(#grid)">')
    for k in range(length - 1, -1, -1):
        seq = [init[k - t] if t < k else heads[t - k] for t in range(len(heads))]
        size = cell - (k * 1.4 if k else 0)
        off = (cell - size) / 2
        path = "M " + " L ".join(f"{xy(c)[0]} {xy(c)[1]}" for c in seq)
        opacity = 1 - k * 0.12
        p.append(
            f'<rect x="{off:.1f}" y="{off:.1f}" width="{size:.1f}" '
            f'height="{size:.1f}" rx="{3 if k else 4}" fill="{SNAKE}" '
            f'opacity="{opacity:.2f}">'
            f'<animateMotion dur="{dur:.1f}s" repeatCount="indefinite" '
            f'calcMode="linear" path="{path}"/></rect>')
    # eyes on the head
    eyes = "M " + " L ".join(f"{xy(c)[0]} {xy(c)[1]}" for c in heads)
    p.append(f'<g><animateMotion dur="{dur:.1f}s" repeatCount="indefinite" '
             f'calcMode="linear" path="{eyes}"/>'
             f'<circle cx="4.2" cy="4.5" r="1.6" fill="{BG}"/>'
             f'<circle cx="8.8" cy="4.5" r="1.6" fill="{BG}"/></g>')
    p.append("</g>")
    p.append("</svg>")
    return "\n".join(p)


# ------------------------------------------------------------------ main ---

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=os.environ.get("GITHUB_USER", "notolac"))
    ap.add_argument("--out", default="assets")
    ap.add_argument("--demo", action="store_true",
                    help="render with synthetic data (preview only)")
    args = ap.parse_args()

    token = (os.environ.get("GITHUB_TOKEN")
             or os.environ.get("GH_TOKEN")
             or None)
    os.makedirs(args.out, exist_ok=True)

    if args.demo:
        user = {"login": args.user, "public_repos": 7, "followers": 5,
                "following": 12}
        repos: list[dict] = []
        stars = 5
        window, history = demo_calendar()
        totals = {"Python": 300000, "Shell": 53000, "HTML": 18000,
                  "Dockerfile": 9000, "JavaScript": 6000, "CSS": 3000}
    else:
        user = fetch_user(token, args.user)
        user.setdefault("login", args.user)
        created = None
        if user.get("created_at"):
            created = dt.date.fromisoformat(user["created_at"][:10])
        repos = fetch_repos(token, args.user)
        stars = sum(int(r.get("stargazers_count", 0) or 0) for r in repos)
        totals = fetch_languages(token, repos)
        window, history = fetch_calendar(token, args.user, created)

    s = streaks(history)
    last_year = sum(d["count"] for d in window)

    cards = {
        "stats.svg": render_stats(user, stars, repos, last_year),
        "streak.svg": render_streak(s),
        "top-langs.svg": render_top_langs(totals),
        "snake.svg": render_snake(window),
    }
    for name, svg in cards.items():
        path = os.path.join(args.out, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg + "\n")
        print(f"wrote {path}")
    print(f"user={user.get('login')} stars={stars} last_year={last_year} "
          f"total={s.total} streak={s.current}/{s.longest} "
          f"langs={len(totals)} window_days={len(window)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
