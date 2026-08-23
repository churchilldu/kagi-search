#!/usr/bin/env python3
"""
kagi-search: Web search via Kagi using the logged-in LibreWolf session cookie.

Pure HTTP + SSE — no headless browser, no geckodriver, no API key. It reads the
Kagi session cookie from LibreWolf's cookies.sqlite and replays two requests the
real web app makes:

    1. GET /search?q=...            (warms up fresh cookies, validates session)
    2. GET /socket/search?q=...&nonce=...  (Server-Sent Events -> results JSON)

Stdlib only (sqlite3 + urllib). Infrastructure reverse-engineered from a HAR
capture of a manual LibreWolf search.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import secrets
import sqlite3
import sys
import urllib.parse
import urllib.request

DOMAIN = "kagi.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 "
      "Firefox/153.0")


class KagiError(Exception):
    pass


# --------------------------------------------------------------------------- #
# profile / cookie discovery
# --------------------------------------------------------------------------- #
def profile_candidates() -> list:
    if os.environ.get("KAGI_PROFILE"):
        return [os.environ["KAGI_PROFILE"]]
    appdata = os.environ.get("APPDATA", "")
    ini = os.path.join(appdata, "librewolf", "profiles.ini")
    base = os.path.join(appdata, "librewolf", "Profiles")
    dirs = {}
    default_mark = None
    if os.path.exists(ini):
        with open(ini, "r", encoding="utf-8", errors="replace") as f:
            sec, path, locked = None, None, False
            for raw in f:
                line = raw.strip()
                if line.startswith("[") and line.endswith("]"):
                    if sec and path:
                        dirs[os.path.basename(path)] = locked
                    sec, path, locked = line[1:-1], None, False
                elif line.startswith("Path="):
                    path = line.split("=", 1)[1].replace("/", os.sep)
                elif line.startswith("Locked="):
                    locked = line.split("=", 1)[1].strip().lower() == "1"
                elif line.startswith("Default=") and path:
                    default_mark = os.path.basename(path)
            if sec and path:
                dirs[os.path.basename(path)] = locked
    ordered = [p for p, _ in sorted(dirs.items(), key=lambda kv: (not kv[1]))]
    if default_mark and default_mark in dirs:
        ordered.insert(0, ordered.pop(ordered.index(default_mark)))
    seen, out = set(), []
    for d in ordered:
        p = os.path.join(base, d)
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            out.append(p)
    return out


def load_cookies(profiles: list) -> dict:
    """Return the Kagi cookies {name:(value,domain,path)} from a profile with
    a valid session, or raise KagiError."""
    for profile in profiles:
        db = os.path.join(profile, "cookies.sqlite")
        if not os.path.exists(db):
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        except Exception:
            continue
        try:
            rows = con.execute(
                "SELECT host,name,value,path FROM moz_cookies "
                "WHERE host LIKE '%kagi.com' and name='kagi_session' "
                "   OR host LIKE '%kagi.com' and name like '_kagi_%' "
                "   OR host LIKE '%kagi.com' and name='kagi_previous_page' "
                "   OR host LIKE '%kagi.com' and name like 'kagi_%'"
            ).fetchall()
            cookies = {}
            for host, name, value, path in rows:
                if name not in cookies and len(value or "") > 0:
                    cookies[name] = (value, host, path)
            if cookies.get("kagi_session"):
                return cookies
        finally:
            con.close()
    return {}


# --------------------------------------------------------------------------- #
# HTTP helper
# --------------------------------------------------------------------------- #
def http_get(url: str, headers: dict, timeout: int = 40):
    req = urllib.request.Request(url, headers=headers)
    resp = urllib.request.urlopen(req, timeout=timeout)
    data = resp.read()
    if resp.headers.get("Content-Encoding") == "gzip":
        data = gzip.decompress(data)
    return resp, data


def fetch_search_html(query: str, lens: str, cookie_str: str) -> dict:
    """GET /search — warms cookies, returns refreshed cookie dict + final status."""
    params = urllib.parse.urlencode({"q": query, **({"lens": lens} if lens else {})})
    url = f"https://{DOMAIN}/search?" + params
    try:
        resp, data = http_get(url, {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"https://{DOMAIN}/",
            "Cookie": cookie_str,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Upgrade-Insecure-Requests": "1",
        }, timeout=40)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise KagiError("Kagi session rejected (HTTP %d). Log in at kagi.com in LibreWolf." % e.code)
        raise KagiError(f"search page request failed: HTTP {e.code}")
    if url.replace(f"https://{DOMAIN}/search", "") and "signin" in resp.geturl():
        raise KagiError("Kagi redirected to Sign In — session expired. Log in again in LibreWolf.")

    # refresh cookies from Set-Cookie headers
    fresh = {}
    for sc in resp.headers.get_all("Set-Cookie", []):
        m = re.match(r"([^=]+)=([^;]*)(;|$)", sc)
        if m and m.group(1) in ("kagi_session", "_kagi_search_", "kagi_previous_page"):
            fresh[m.group(1)] = m.group(2)
    return {"html": data, "refresh": fresh, "params": params}


def fetch_socket(query: str, lens: str, cookie_str: str, session: str) -> str:
    """GET /socket/search — SSE stream of results JSON. Returns raw stream text."""
    params = urllib.parse.urlencode({"q": query, **({"lens": lens} if lens else {})})
    nonce = secrets.token_hex(16)
    url = f"https://{DOMAIN}/socket/search?{params}&nonce={nonce}"
    try:
        resp, data = http_get(url, {
            "User-Agent": UA,
            "Accept": "text/event-stream",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"https://{DOMAIN}/search?{params}",
            "Cookie": cookie_str,
            "X-Kagi-Authorization": session,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Priority": "u=4",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }, timeout=60)
    except urllib.error.HTTPError as e:
        raise KagiError(f"socket request failed: HTTP {e.code}")
    return data.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# SSE parsing
# --------------------------------------------------------------------------- #
def parse_sse(stream: str) -> dict:
    payloads = {}
    buffer = ""
    for line in stream.split("\n"):
        if line.startswith("data: "):
            buffer += line[6:]
        elif line == "" and buffer:
            try:
                arr = json.loads(buffer)
                for item in arr:
                    if isinstance(item, dict) and "tag" in item:
                        payloads.setdefault(item["tag"], []).append(item["payload"])
            except Exception:
                pass
            buffer = ""
    return payloads


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s).strip()


def parse_related(html: str):
    spans = re.findall(r"<span>(.*?)</span>", html or "", re.S)
    return [strip_html(s) for s in spans if strip_html(s)]


def run_search(query: str, limit: int, lens: str, with_related: bool) -> dict:
    cookies = load_cookies(profile_candidates())
    if not cookies:
        raise KagiError(
            "No LibreWolf profile with a Kagi login found.\n"
            "  - Log in at https://kagi.com in LibreWolf, then retry.\n"
            "  - Or set KAGI_PROFILE to the profile dir holding your Kagi cookies."
        )
    session = cookies["kagi_session"][0]
    old_search = cookies.get("_kagi_search_", ("", None, None))[0]
    base_cookie = f"kagi_session={session}"
    if old_search:
        base_cookie = f"_kagi_search_={old_search}; {base_cookie}"

    # 1) warm up + refresh cookies (also validates the session)
    warm = fetch_search_html(query, lens, base_cookie)
    ref = warm["refresh"]
    now = ref.get("kagi_session", session)
    now_search = ref.get("_kagi_search_", old_search)
    cookie_now = f"kagi_session={now}"
    if now_search:
        cookie_now = f"_kagi_search_={now_search}; {cookie_now}"
    # prefer refreshed session if the server issued one
    session = now

    # 2) SSE results
    stream = fetch_socket(query, lens, cookie_now, session)
    payloads = parse_sse(stream)

    items = []
    answer_box = None
    quick = ""
    for payload in payloads.get("search_results_json", []):
        try:
            p = json.loads(payload)
        except Exception:
            continue
        items = p.get("items") or items
        answer_box = p.get("answer_box") or answer_box
    for payload in payloads.get("top_content", []):
        if not isinstance(payload, str):
            continue
        m = re.search(r"_0_qa_reply_box\">(.*?)</div>", payload, re.S)
        if m:
            quick = strip_html(m.group(1))

    results = []
    for it in items or []:
        results.append({
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "snippet": (it.get("snippet") or "").strip(),
            "source": it.get("source", ""),
            "rank": it.get("rank", 0),
        })
    out = {
        "query": query,
        "results": results[:limit],
        "quick": quick,
        "answer_box": answer_box,
        "related": parse_related(
            payloads.get("related_searches", [None])[0]
            if with_related and payloads.get("related_searches")
            else ""
        ),
    }
    return out


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
def render_markdown(data: dict, with_quick: bool) -> str:
    lines = []
    if with_quick and data.get("quick"):
        lines.append("## Quick Answer")
        lines.append(data["quick"])
        lines.append("")
    for i, r in enumerate(data.get("results") or [], 1):
        lines.append(f"--- Result {i} ---")
        lines.append(f"Title: {r['title']}")
        lines.append(f"Link: {r['url']}")
        if r.get("snippet"):
            lines.append(f"Snippet: {r['snippet']}")
        if r.get("source"):
            lines.append(f"Source: {r['source']}")
        lines.append("")
    rel = [x for x in (data.get("related") or []) if x]
    if rel:
        lines.append("## Related searches")
        lines.append("\n".join(f"- {r}" for r in rel))
    return "\n".join(lines).rstrip()


def main():
    ap = argparse.ArgumentParser(description="Kagi web search via your LibreWolf login cookie (pure HTTP, no API key).")
    ap.add_argument("query", nargs="?", help="search query")
    ap.add_argument("-n", "--limit", type=int, default=10, help="max results (default 10)")
    ap.add_argument("--offset", type=int, default=0, help="skip this many results")
    ap.add_argument("--lens", default="", help="Kagi lens, e.g. code, videos, news, forums, academic")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--no-related", action="store_true", help="hide related searches")
    ap.add_argument("--no-quick", action="store_true", help="omit the quick answer block")
    args = ap.parse_args()
    if not args.query:
        ap.print_help()
        sys.exit(1)

    with_related = not args.no_related
    with_quick = not args.no_quick
    try:
        data = run_search(args.query, args.offset + args.limit, args.lens, with_related)
        data["results"] = data["results"][args.offset:]
    except KagiError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(data, with_quick))


if __name__ == "__main__":
    main()