#!/usr/bin/env python3
"""Download official-site favicons for the tech-stack README.

Icons land in ``assets/icons/<slug>.png`` (128px). Prefer Google's public
favicon endpoint; fall back to DuckDuckGo. For inherently monochrome brand
marks (Apple, GitHub, Bash, Cursor, …) also keep a white SVG from
simple-icons so they stay visible on GitHub's dark README.

    python3 tools/fetch_icons.py            # fetch missing only
    python3 tools/fetch_icons.py --force    # re-download everything
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.request

# slug -> official domain used as the favicon source
ICONS = {
    "vmware": "vmware.com",
    "proxmox": "proxmox.com",
    "hostinger": "hostinger.com",
    "kali": "kali.org",
    "parrotos": "parrotsec.org",
    "apps-script": "developers.google.com",
    "wireguard": "wireguard.com",
    "openvpn": "openvpn.net",
    "anyrun": "any.run",
    "sentinelone": "sentinelone.com",
    "cultureai": "culture.ai",
    "google-secops": "cloud.google.com",
    "onelogin": "onelogin.com",
    "okta": "okta.com",
    "google-workspace": "workspace.google.com",
    "odoo": "odoo.com",
    "ghost": "ghost.org",
    "n8n": "n8n.io",
    "make": "make.com",
    "jira": "atlassian.com",
    "aws": "aws.amazon.com",
    "gcp": "cloud.google.com",
    "openai": "openai.com",
    "claude": "claude.ai",
    "gemini": "gemini.google.com",
    "vertexai": "cloud.google.com",
    "ai-studio": "aistudio.google.com",
    "litellm": "www.litellm.ai",
    "ollama": "ollama.com",
    "notebooklm": "notebooklm.google.com",
    "perplexity": "perplexity.ai",
    "rovo": "www.atlassian.com",
    "hermes": "hermes-agent.nousresearch.com",
    "opencode": "opencode.ai",
    "cursor": "cursor.com",
    "apple": "apple.com",
    "github": "github.com",
    "bash": "www.gnu.org",
    "django": "www.djangoproject.com",
    "ansible": "www.ansible.com",
}

# Prefer the site's own apple-touch / product icon when Google's cache is
# the wrong brand (or a generic G / Windows logo).
DIRECT = {
    "hermes": "https://hermes-agent.nousresearch.com/icon.png",
    "litellm": ("https://cdn.prod.website-files.com/69eb241da3f923869c226875/"
                "69eb2dbc67b72fe279d69579_Logo.webp"),
    "opencode": "https://opencode.ai/apple-touch-icon-v3.png",
    "cursor": "https://cursor.com/marketing-static/icon-192x192-light.png",
    "anyrun": "https://files.any.run/images/favicon-ws/apple-touch-icon.png",
    "ai-studio": ("https://www.gstatic.com/images/branding/productlogos/"
                  "ai_studio/v1/web-512dp/logo_ai_studio_color_1x_web_512dp.png"),
}

# Brand marks that ship as black silhouettes. Store a white SVG locally so
# they remain visible on GitHub dark mode (simple-icons default fill is #000).
WHITE = {
    "apple": "apple",
    "github": "github",
    "bash": "gnubash",
    "cursor": "cursor",
    "opencode": "opencode",
    "ansible": "ansible",
    "django": "django",
}

GOOGLE = "https://www.google.com/s2/favicons?domain={domain}&sz=128"
DDG = "https://icons.duckduckgo.com/ip3/{domain}.ico"
SIMPLE = "https://cdn.simpleicons.org/{slug}/ffffff"


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url: str, op: urllib.request.OpenerDirector) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 notolac-profile-icons/1.0"})
    with op.open(req, timeout=30) as res:
        return res.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="assets/icons")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    op = opener()
    failed = 0

    for slug, domain in ICONS.items():
        path = os.path.join(args.out, f"{slug}.png")
        if os.path.exists(path) and not args.force:
            continue
        urls = []
        if slug in DIRECT:
            urls.append(DIRECT[slug])
        urls += [GOOGLE.format(domain=domain), DDG.format(domain=domain)]
        data = b""
        for url in urls:
            try:
                data = fetch(url, op)
                if len(data) >= 100:
                    break
            except Exception as exc:
                print(f"warn {slug} {url}: {exc}", file=sys.stderr)
                data = b""
        if len(data) < 100:
            print(f"FAIL {slug} ({domain}): empty/tiny response", file=sys.stderr)
            failed += 1
            continue
        with open(path, "wb") as fh:
            fh.write(data)
        print(f"ok   {slug:<18} {domain:<36} {len(data):>6} bytes")

    for slug, si in WHITE.items():
        path = os.path.join(args.out, f"{slug}-white.svg")
        if os.path.exists(path) and not args.force:
            continue
        try:
            data = fetch(SIMPLE.format(slug=si), op)
        except Exception as exc:
            print(f"FAIL {slug}-white: {exc}", file=sys.stderr)
            failed += 1
            continue
        with open(path, "wb") as fh:
            fh.write(data)
        print(f"ok   {slug:<18} simple-icons/{si} white     {len(data):>6} bytes")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
