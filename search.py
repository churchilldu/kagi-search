#!/usr/bin/env python3
"""
kagi-search: Web search via Kagi using a logged-in browser session cookie.

Pure HTTP + SSE — no headless browser, no geckodriver, no API key. It reads the
Kagi session cookie out of whichever browser you are logged in with (via
browser_cookie3) and replays two requests the real web app makes:

    1. GET /search?q=...            (warms up fresh cookies, validates session)
    2. GET /socket/search?q=...&nonce=...  (Server-Sent Events -> results JSON)

Requires browser_cookie3; everything else is stdlib. Infrastructure
reverse-engineered from a HAR capture of a manual LibreWolf search.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request

# Windows defaults stdout to cp1252, which cannot encode arbitrary Unicode in
# search results (snippets, titles) and crashes with UnicodeEncodeError.
# Force UTF-8 (best effort) so the script works no matter how it is invoked.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DOMAIN = "kagi.com"
# Browsers probed in order; the first one holding a kagi_session wins.
# LibreWolf/Firefox first because Kagi cookies are plaintext there (no key
# decryption) and that is the setup this skill was built on.
BROWSER_ORDER = ("librewolf", "firefox", "chrome", "edge", "brave",
                 "chromium", "vivaldi", "opera", "safari")
GECKO_BROWSERS = ("librewolf", "firefox")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 "
      "Firefox/153.0")
CACHE_FILENAME = ".kagi_cache.json"
# When the stored cookie has no real expiry (0 / session cookie), fall back to
# this TTL and re-read from the browser after it passes.
DEFAULT_TTL_SECONDS = 6 * 60 * 60  # 6 hours
CACHE_KEYS = ("kagi_session", "_kagi_search_", "kagi_previous_page")


class KagiError(Exception):
    pass


# --------------------------------------------------------------------------- #
# browser cookie discovery
# --------------------------------------------------------------------------- #
def skill_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def cache_file() -> str:
    return os.environ.get("KAGI_CACHE_FILE") or os.path.join(skill_dir(), CACHE_FILENAME)


def read_cookie_cache(now: float = None) -> dict:
    """Return {name:value,...} from the cache file if un-expired, else {}."""
    now = time.time() if now is None else now
    try:
        with open(cache_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict) or not data.get("kagi_session"):
        return {}
    exp = data.get("expires_at") or 0
    if exp > now:
        return {k: data[k] for k in CACHE_KEYS if data.get(k)}
    return {}


def write_cookie_cache(cookies: dict, expires_at: float) -> None:
    try:
        payload = {k: v for k, v in cookies.items() if k in CACHE_KEYS and v}
        payload["expires_at"] = expires_at or time.time() + DEFAULT_TTL_SECONDS
        payload["fetched_at"] = time.time()
        tmp = cache_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, cache_file())
    except Exception:
        pass  # cache is best-effort; never block the search


def _kagi_cookies_from_jar(jar) -> tuple:
    """Pick the Kagi cookies out of a cookiejar -> ({name: value}, expiry_sec)."""
    cookies, session_expiry = {}, None
    for cookie in jar:
        if not cookie.value or cookie.name in cookies:
            continue
        if cookie.name not in CACHE_KEYS:
            continue
        cookies[cookie.name] = cookie.value
        if cookie.name == "kagi_session":
            session_expiry = _expiry_seconds(cookie.expires)
    return cookies, session_expiry


def _expiry_seconds(expires):
    """Normalize a cookie expiry to epoch seconds, or None for session cookies.

    browser_cookie3 hands Firefox-based cookies through with their raw
    millisecond `moz_cookies.expiry`; Chromium-based ones are in seconds."""
    if not expires or expires <= 0:
        return None
    return expires / 1000.0 if expires > 1e11 else float(expires)


def _cookie_readers() -> list:
    """Browser names to probe: KAGI_BROWSER if set, else BROWSER_ORDER."""
    forced = (os.environ.get("KAGI_BROWSER") or "").strip().lower()
    return [forced] if forced else list(BROWSER_ORDER)


def read_browser_cookies() -> tuple:
    """Return (cookies, kagi_session_expiry) from the first browser with a login.

    Raises KagiError when no browser yields a Kagi session, listing what was
    tried so a failed decryption is debuggable."""
    try:
        import browser_cookie3
    except ImportError as exc:
        raise KagiError(
            "browser_cookie3 is required to read browser cookies.\n"
            "  Install it with: pip install --user browser-cookie3"
        ) from exc

    profile = os.environ.get("KAGI_PROFILE")
    attempted = []
    for browser in _cookie_readers():
        reader = getattr(browser_cookie3, browser, None)
        if reader is None:
            attempted.append(f"{browser}: unknown browser")
            continue
        kwargs = {"domain_name": DOMAIN}
        if profile and browser in GECKO_BROWSERS:
            kwargs["cookie_file"] = os.path.join(profile, "cookies.sqlite")
        try:
            jar = reader(**kwargs)
        except Exception as exc:  # missing profile, locked db, bad key, ...
            attempted.append(f"{browser}: {type(exc).__name__}: {exc}")
            continue
        cookies, expiry = _kagi_cookies_from_jar(jar)
        if cookies.get("kagi_session"):
            return cookies, expiry
        attempted.append(f"{browser}: no kagi_session cookie")

    raise KagiError(
        "No Kagi session cookie found in any browser.\n"
        "  - Log in at https://kagi.com in your browser, then retry.\n"
        "  - Tried: " + ", ".join(attempted) + "\n"
        "  - Set KAGI_BROWSER to pick one browser (" + "|".join(BROWSER_ORDER) + ").\n"
        "  - Set KAGI_PROFILE to a Firefox/LibreWolf profile dir if detection fails."
    )


def load_cookies(use_cache: bool = True) -> dict:
    """Return the Kagi cookies {name:value,...} for a valid session.

    Uses the on-disk cache file first; only reads the browser (and refreshes the
    cache) when the cache is missing or expired. Never raise when cache breaks."""
    if use_cache:
        cached = read_cookie_cache()
        if cached:
            return cached
    cookies, expiry = read_browser_cookies()
    write_cookie_cache(cookies, expiry)
    return cookies


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
            raise KagiError("Kagi session rejected (HTTP %d). Log in at kagi.com in your browser." % e.code)
        raise KagiError(f"search page request failed: HTTP {e.code}")
    if url.replace(f"https://{DOMAIN}/search", "") and "signin" in resp.geturl():
        raise KagiError("Kagi redirected to Sign In — session expired. Log in again in your browser.")

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


def run_search(query: str, limit: int, lens: str, with_related: bool,
               use_cache: bool = True) -> dict:
    cookies = load_cookies(use_cache=use_cache)
    session = cookies["kagi_session"]
    old_search = cookies.get("_kagi_search_", "")
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
    # persist refreshed cookies back to the cache so it stays current
    if use_cache and now and now != cookies.get("kagi_session"):
        write_cookie_cache({"kagi_session": now, "_kagi_search_": now_search}, 0)

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
    ap = argparse.ArgumentParser(description="Kagi web search via your browser login cookie (pure HTTP, no API key).")
    ap.add_argument("query", nargs="?", help="search query")
    ap.add_argument("-n", "--limit", type=int, default=10, help="max results (default 10)")
    ap.add_argument("--offset", type=int, default=0, help="skip this many results")
    ap.add_argument("--lens", default="", help="Kagi lens, e.g. code, videos, news, forums, academic")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--no-related", action="store_true", help="hide related searches")
    ap.add_argument("--no-quick", action="store_true", help="omit the quick answer block")
    ap.add_argument("--browser", default="",
                    help="read cookies from this browser only (" + ", ".join(BROWSER_ORDER) + ")")
    ap.add_argument("--no-cache", action="store_true",
                    help="ignore the cached cookie and re-read the browser")
    args = ap.parse_args()
    if args.browser:
        os.environ["KAGI_BROWSER"] = args.browser
    if not args.query:
        ap.print_help()
        sys.exit(1)

    with_related = not args.no_related
    with_quick = not args.no_quick
    try:
        data = run_search(args.query, args.offset + args.limit, args.lens,
                          with_related, use_cache=not args.no_cache)
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