#!/usr/bin/env python3
"""Self-hosted GitHub profile stats generator — zero third-party services.

Renders local SVG cards from public GitHub data:

    assets/stats.svg     — stars, contributions, repos, followers
    assets/top-langs.svg — most used languages across owned repos
    assets/streak.svg    — current / longest contribution streak
    assets/activity.svg  — full-year contribution heatmap
    assets/snake.svg     — contribution-grid snake animation

Data sources: the GitHub REST/GraphQL APIs (with the standard
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
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
GQL = "https://api.github.com/graphql"

# ---------------------------------------------------------------- theme ---

BG = "#0d1117"
BORDER = "#21262d"
TITLE = "#58a6ff"
TEXT = "#e6edf3"
MUTED = "#7d8590"
GREEN = "#39d353"
PURPLE = "#a371f7"
BLUE = "#1f6feb"
ORANGE = "#f78166"

# GitHub dark-mode contribution scale.
SCALE = ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"]

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

FONT = "font-family=\"-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif\""


def esc(s: object) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


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
        req.add_header("User-Agent", "notolac-profile-stats/1.0")
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


def _calendar_from_gql(token: str, user: str) -> tuple[list[list[dict]], int]:
    query = """
    query ($login: String!) {
      user(login: $login) {
        contributionsCollection {
          contributionCalendar {
            totalContributions
            weeks { contributionDays { date contributionCount } }
          }
        }
      }
    }"""
    res = _req(GQL, token, method="POST",
               payload={"query": query, "variables": {"login": user}})
    assert isinstance(res, dict)
    cal = res["data"]["user"]["contributionsCollection"][
        "contributionCalendar"]
    weeks = [[{"date": d["date"], "count": d["contributionCount"]}
              for d in w["contributionDays"]]
             for w in cal["weeks"]]
    return weeks, int(cal["totalContributions"])


def _calendar_from_html(user: str) -> tuple[list[list[dict]], int]:
    """Token-free fallback: parse the public contributions page.

    Cells carry ``data-date``/``data-level``; the exact count lives in the
    associated ``tool-tip`` (``for`` attribute → cell ``id``).
    """
    url = f"https://github.com/users/{user}/contributions"
    req = urllib.request.Request(
        url, headers={"User-Agent": "notolac-profile-stats/1.0"})
    with _OPENER.open(req, timeout=30) as res:
        html = res.read().decode("utf-8", "replace")

    cells = re.findall(
        r'<td[^>]*id="(contribution-day-component-[^"]+)"[^>]*?'
        r'data-date="(\d{4}-\d{2}-\d{2})"[^>]*?data-level="(\d)"', html)
    if not cells:  # attribute order fallback
        cells = [(m.group(2), m.group(1), m.group(3)) for m in re.finditer(
            r'<td[^>]*data-date="(\d{4}-\d{2}-\d{2})"[^>]*?'
            r'id="(contribution-day-component-[^"]+)"[^>]*?'
            r'data-level="(\d)"', html)]
    tips = dict(re.findall(
        r'<tool-tip[^>]*for="(contribution-day-component-[^"]+)"[^>]*>'
        r'([^<]*)</tool-tip>', html))

    days: list[dict] = []
    for cid, date, lvl in cells:
        tip = tips.get(cid, "")
        m = re.match(r"([\d,]+) contribution", tip)
        count = int(m.group(1).replace(",", "")) if m else 0
        days.append({"date": date, "count": count})

    total_m = re.search(r"([\d,]+)\s*contributions\s*in\s*the\s*last\s*year",
                        html)
    total = (int(total_m.group(1).replace(",", "")) if total_m
             else sum(d["count"] for d in days))
    if not days:
        raise RuntimeError("no contribution cells found in HTML")

    # group into weeks (Sunday-first columns), ordered by date
    days.sort(key=lambda d: d["date"])
    weeks: list[list[dict]] = []
    for d in days:
        row = (dt.date.fromisoformat(d["date"]).weekday() + 1) % 7
        if row == 0 or not weeks:
            weeks.append([])
        weeks[-1].append(d)
    return weeks, total


def fetch_calendar(token: str | None, user: str) -> tuple[list[list[dict]], int]:
    """Return (weeks, total_contributions). Weeks are lists of day dicts
    ``{"date": "YYYY-MM-DD", "count": int}``."""
    if token:
        try:
            return _calendar_from_gql(token, user)
        except Exception as exc:
            print(f"warn: calendar via GraphQL failed ({exc}), "
                  "falling back to public HTML", file=sys.stderr)
    try:
        return _calendar_from_html(user)
    except Exception as exc:
        print(f"warn: calendar fetch failed ({exc})", file=sys.stderr)
        return [], 0


def demo_calendar() -> tuple[list[list[dict]], int]:
    """Synthetic year of data for layout previews (``--demo``)."""
    import random
    random.seed(7)
    today = dt.date.today()
    start = today - dt.timedelta(days=364)
    start -= dt.timedelta(days=(start.weekday() + 1) % 7)  # align to Sunday
    weeks: list[list[dict]] = []
    day = start
    while day <= today:
        week = []
        for _ in range(7):
            if day <= today:
                count = random.choices([0, 0, 0, 1, 2, 3, 5, 9],
                                       weights=[30, 20, 10, 15, 10, 7, 5, 3])[0]
                week.append({"date": day.isoformat(), "count": count})
            day += dt.timedelta(days=1)
        weeks.append(week)
    # guarantee an active streak ending today
    for week in reversed(weeks):
        for d in reversed(week):
            if d["date"] <= today.isoformat():
                d["count"] = max(d["count"], 2)
                if d["date"] <= (today - dt.timedelta(days=5)).isoformat():
                    break
        else:
            continue
        break
    total = sum(d["count"] for w in weeks for d in w)
    return weeks, total


# --------------------------------------------------------------- streaks ---

def streaks(weeks: list[list[dict]]) -> tuple[int, int, int]:
    """Return (current_streak, longest_streak, total) in days."""
    days = sorted((d for w in weeks for d in w), key=lambda d: d["date"])
    if not days:
        return 0, 0, 0
    total = sum(d["count"] for d in days)
    longest = run = 0
    for d in days:
        run = run + 1 if d["count"] > 0 else 0
        longest = max(longest, run)
    today = dt.date.today().isoformat()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    current = 0
    for d in reversed(days):
        if d["date"] > today or d["count"] == 0:
            if d["date"] > today:
                continue
            if current == 0 and d["date"] >= yesterday:
                continue  # streak alive through yesterday
            break
        current += 1
    return current, longest, total


def level(count: int) -> int:
    if count == 0:
        return 0
    if count == 1:
        return 1
    if count <= 3:
        return 2
    if count <= 5:
        return 3
    return 4


# ------------------------------------------------------------------ svg ---

def card_open(w: int, h: int, title: str, uid: str,
              accent_a: str = BLUE, accent_b: str = PURPLE) -> list[str]:
    """Open a card with a gradient background and an accent title bar."""
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" {FONT} role="img">',
        "<defs>",
        f'<linearGradient id="bg-{uid}" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="#161b22"/>'
        f'<stop offset="1" stop-color="{BG}"/></linearGradient>',
        f'<linearGradient id="ac-{uid}" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{accent_a}"/>'
        f'<stop offset="1" stop-color="{accent_b}"/></linearGradient>',
        "</defs>",
        f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="8" '
        f'fill="url(#bg-{uid})" stroke="{BORDER}"/>',
        f'<rect x="0.5" y="0.5" width="{w - 1}" height="3" rx="1.5" '
        f'fill="url(#ac-{uid})"/>',
        f'<text x="20" y="34" font-size="15" font-weight="700" '
        f'fill="{TITLE}">{esc(title)}</text>',
        f'<line x1="20" y1="44" x2="{w - 20}" y2="44" stroke="{BORDER}" '
        f'stroke-width="1"/>',
    ]


def card_footer(w: int, h: int, updated: str) -> list[str]:
    return [
        f'<text x="{w - 12}" y="{h - 10}" font-size="10" fill="{MUTED}" '
        f'text-anchor="end">updated {esc(updated)}</text>',
        '<style>@media (prefers-color-scheme: light) {'
        f' text {{ fill: #1f2328 !important; }}'
        f' .muted {{ fill: #57606a !important; }}'
        "}</style>",
        "</svg>",
    ]


# -------------------------------------------------------------- renderers ---

def render_stats(user: dict, stars: int, total: int, updated: str) -> str:
    w, h = 420, 200
    rows = [
        ("★", GREEN, "Total stars", stars),
        ("▦", PURPLE, "Contributions (last year)", total if total else "—"),
        ("▣", TITLE, "Public repositories", user.get("public_repos", "—")),
        ("◉", ORANGE, "Followers", user.get("followers", "—")),
    ]
    p = card_open(w, h, f"{user.get('login', 'notolac')}'s GitHub stats",
                  "stats", GREEN, TITLE)
    y = 70
    for icon, color, label, value in rows:
        p.append(f'<circle cx="26" cy="{y - 4}" r="9" fill="{color}" '
                 f'opacity="0.18"/>')
        p.append(f'<text x="26" y="{y}" font-size="12" fill="{color}" '
                 f'text-anchor="middle">{icon}</text>')
        p.append(f'<text x="44" y="{y}" font-size="13" fill="{MUTED}">'
                 f'{label}</text>')
        p.append(f'<text x="{w - 24}" y="{y}" font-size="14" font-weight="700" '
                 f'fill="{TEXT}" text-anchor="end">{esc(value)}</text>')
        y += 30
    p += card_footer(w, h, updated)
    return "\n".join(p)


def render_top_langs(totals: dict[str, int], updated: str) -> str:
    w = 340
    top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:6]
    h = 56 + len(top) * 36 + 26
    p = card_open(w, h, "Most used languages", "langs", ORANGE, GREEN)
    y = 66
    for lang, nbytes in top:
        color = LANG_COLORS.get(lang, MUTED)
        pct = 100 * nbytes / sum(totals.values()) if totals else 0
        bar = 200 * nbytes / (top[0][1] or 1)
        p.append(f'<circle cx="26" cy="{y - 4}" r="5" fill="{color}"/>')
        p.append(f'<text x="38" y="{y}" font-size="12" font-weight="600" '
                 f'fill="{TEXT}">{esc(lang)}</text>')
        p.append(f'<text x="{w - 20}" y="{y}" font-size="12" fill="{MUTED}" '
                 f'text-anchor="end">{pct:.1f}%</text>')
        p.append(f'<rect x="20" y="{y + 7}" width="200" height="6" rx="3" '
                 f'fill="#21262d"/>')
        p.append(f'<rect x="20" y="{y + 7}" width="{bar:.1f}" height="6" rx="3" '
                 f'fill="{color}"/>')
        y += 36
    if not top:
        p.append(f'<text x="20" y="70" font-size="12" fill="{MUTED}">'
                 "no language data yet</text>")
    p += card_footer(w, h, updated)
    return "\n".join(p)


def render_streak(current: int, longest: int, total: int, updated: str) -> str:
    w, h = 420, 200
    p = card_open(w, h, "Contribution streak", "streak", ORANGE, PURPLE)
    cells = [
        ("Current streak", f"{current} day{'s' if current != 1 else ''}",
         ORANGE),
        ("Longest streak", f"{longest} day{'s' if longest != 1 else ''}",
         GREEN),
        ("Total (last year)", str(total), TITLE),
    ]
    x = 30
    for label, value, color in cells:
        p.append(f'<rect x="{x - 12}" y="58" width="120" height="76" rx="8" '
                 f'fill="{color}" opacity="0.10"/>')
        p.append(f'<rect x="{x - 12}" y="58" width="120" height="3" rx="1.5" '
                 f'fill="{color}"/>')
        p.append(f'<text x="{x}" y="96" font-size="24" font-weight="700" '
                 f'fill="{color}">{esc(value)}</text>')
        p.append(f'<text x="{x}" y="118" font-size="11" fill="{MUTED}">'
                 f'{esc(label)}</text>')
        x += 135
    p += card_footer(w, h, updated)
    return "\n".join(p)


def render_activity(weeks: list[list[dict]], updated: str) -> str:
    cell, gap = 11, 4
    step = cell + gap
    pad_x, top, bottom = 16, 48, 34
    cols = max(len(weeks), 1)
    gw = cols * step - gap
    w = pad_x * 2 + gw
    h = top + 7 * step - gap + bottom
    p = card_open(w, h, "Contribution activity", "act", GREEN, TITLE)
    # month labels
    prev_month = ""
    for ci, week in enumerate(weeks):
        if not week:
            continue
        month = dt.date.fromisoformat(week[0]["date"]).strftime("%b")
        if month != prev_month:
            x = pad_x + ci * step
            p.append(f'<text x="{x}" y="{top - 8}" font-size="10" '
                     f'fill="{MUTED}">{month}</text>')
            prev_month = month
    for ci, week in enumerate(weeks):
        for day in week:
            row = (dt.date.fromisoformat(day["date"]).weekday() + 1) % 7
            x = pad_x + ci * step
            y = top + row * step
            color = SCALE[level(day["count"])]
            p.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" '
                     f'rx="2.5" fill="{color}">'
                     f'<title>{day["date"]}: {day["count"]} '
                     f'contributions</title></rect>')
    # legend
    lx = w - pad_x - (5 * step + 70)
    ly = h - 22
    p.append(f'<text x="{lx}" y="{ly + 9}" font-size="10" '
             f'fill="{MUTED}">Less</text>')
    for i, color in enumerate(SCALE):
        p.append(f'<rect x="{lx + 36 + i * step}" y="{ly}" width="{cell}" '
                 f'height="{cell}" rx="2.5" fill="{color}"/>')
    p.append(f'<text x="{lx + 36 + 5 * step + 6}" y="{ly + 9}" font-size="10" '
             f'fill="{MUTED}">More</text>')
    p += card_footer(w, h, updated)
    return "\n".join(p)


def render_snake(weeks: list[list[dict]]) -> str:
    """Serpentine snake travelling the contribution grid (SMIL animation).

    Falls back to an empty 53-week grid when no calendar data is available
    so the animation always has a full board to run on.
    """
    cell, gap = 13, 3
    step = cell + gap
    pad = 14
    cols = max(len(weeks), 53)
    w = pad * 2 + cols * step - gap
    h = pad * 2 + 7 * step - gap
    cx = lambda ci: pad + ci * step + cell / 2  # noqa: E731
    cy = lambda ri: pad + ri * step + cell / 2  # noqa: E731

    # traversal order: column by column, alternating direction
    order: list[tuple[int, int]] = []
    for ci in range(cols):
        rows = range(7) if ci % 2 == 0 else range(6, -1, -1)
        for ri in rows:
            order.append((ci, ri))
    path = "M " + " L ".join(f"{cx(ci):.1f} {cy(ri):.1f}" for ci, ri in order)

    counts = {(ci, ri): 0 for ci in range(cols) for ri in range(7)}
    for ci, week in enumerate(weeks):
        for day in week:
            row = (dt.date.fromisoformat(day["date"]).weekday() + 1) % 7
            counts[(ci, row)] = day["count"]

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
         f'viewBox="0 0 {w} {h}" {FONT} role="img">',
         "<defs>",
         '<linearGradient id="snakebg" x1="0" y1="0" x2="1" y2="1">'
         '<stop offset="0" stop-color="#161b22"/>'
         f'<stop offset="1" stop-color="{BG}"/></linearGradient>',
         "</defs>",
         f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="8" '
         f'fill="url(#snakebg)" stroke="{BORDER}"/>']
    for (ci, ri), count in counts.items():
        x = pad + ci * step
        y = pad + ri * step
        p.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" '
                 f'rx="3" fill="{SCALE[level(count)]}"/>')

    dur = max(8, len(order) // 4)
    body = 12
    for i in range(body, 0, -1):
        r = 6.2 - i * 0.18
        op = 1.0 - i * 0.06
        p.append(
            f'<circle r="{r:.1f}" fill="{PURPLE}" opacity="{op:.2f}">'
            f'<animateMotion dur="{dur}s" repeatCount="indefinite" '
            f'begin="-{i * dur / len(order):.2f}s" path="{path}"/></circle>')
    # head with eyes
    p.append(
        f'<g><animateMotion dur="{dur}s" repeatCount="indefinite" '
        f'path="{path}"/>'
        f'<circle r="7.5" fill="{PURPLE}"/>'
        f'<circle cx="-2.4" cy="-2.6" r="1.7" fill="{BG}"/>'
        f'<circle cx="2.4" cy="-2.6" r="1.7" fill="{BG}"/></g>')
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
    updated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    if args.demo:
        user = {"login": args.user, "public_repos": 7, "followers": 5}
        stars = 5
        weeks, total = demo_calendar()
        totals = {"Python": 300000, "Shell": 53000, "HTML": 18000,
                  "Dockerfile": 9000, "JavaScript": 6000, "CSS": 3000}
    else:
        user = fetch_user(token, args.user)
        user.setdefault("login", args.user)
        repos = fetch_repos(token, args.user)
        stars = sum(int(r.get("stargazers_count", 0) or 0) for r in repos)
        totals = fetch_languages(token, repos)
        weeks, total = fetch_calendar(token, args.user)

    current, longest, _ = streaks(weeks)

    cards = {
        "stats.svg": render_stats(user, stars, total, updated),
        "top-langs.svg": render_top_langs(totals, updated),
        "streak.svg": render_streak(current, longest, total, updated),
        "activity.svg": render_activity(weeks, updated),
        "snake.svg": render_snake(weeks),
    }
    for name, svg in cards.items():
        path = os.path.join(args.out, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(svg + "\n")
        print(f"wrote {path}")
    print(f"user={user.get('login')} stars={stars} contributions={total} "
          f"streak={current}/{longest} langs={len(totals)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
