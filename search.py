#!/usr/bin/env python3
"""
kagi-search: Web search via Kagi using the logged-in LibreWolf session cookie
(scraped from the local browser profile), rendered in a real headless LibreWolf
so Kagi serves full results — no Kagi API key required.

Borrows CLI ergonomics from the community kagi-search/brave-search skills:
  -n/--limit, --offset, --json, --related, output formatting.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

APP_NAME = "kagi-search"
COOKIE_DOMAIN = "kagi.com"
LIBREWOLF_DEFAULTS = {
    "win32": r"C:\Program Files\LibreWolf\librewolf.exe",
    "Windows": r"C:\Program Files\LibreWolf\librewolf.exe",
    "darwin": "/Applications/LibreWolf.app/Contents/MacOS/librewolf",
    "linux": "librewolf",
}


class WDError(Exception):
    pass


class WDClient:
    """Minimal WebDriver (Marionette) HTTP client — stdlib only."""

    def __init__(self, port: int, timeout: int = 240):
        self.base = f"http://127.0.0.1:{port}"
        self.sid = None
        self.timeout = timeout

    def _req(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return json.loads(raw) if raw else {}
            except Exception:
                raise WDError(f"HTTP {e.code}: {raw[:200]!r}")

    def post(self, path, body=None):
        return self._req("POST", path, body)

    def delete(self, path):
        return self._req("DELETE", path)

    def new_session(self, binary: str, extra_args: list):
        caps = {
            "capabilities": {
                "alwaysMatch": {
                    "moz:firefoxOptions": {
                        "binary": binary,
                        "args": ["-headless"] + extra_args,
                    }
                }
            }
        }
        r = self.post("/session", caps)
        v = r.get("value", {})
        if isinstance(v, dict) and "sessionId" in v:
            self.sid = v["sessionId"]
            return v
        raise WDError(f"failed to create browser session: {v}")

    def navigate(self, url: str):
        self.post(f"/session/{self.sid}/url", {"url": url})

    def exec_js(self, script: str):
        r = self.post(
            f"/session/{self.sid}/execute/sync",
            {"script": script, "args": []},
        )
        v = r.get("value")
        if isinstance(r.get("error"), str):
            raise WDError(f"JS error: {r.get('error')}: {r.get('message','')[:200]}")
        return v

    def add_cookie(self, name, value, domain, path, secure, http_only):
        self.post(f"/session/{self.sid}/cookie", {
            "cookie": {
                "name": name, "value": value, "domain": domain, "path": path,
                "secure": secure, "httpOnly": http_only,
            }
        })

    def close(self):
        if self.sid:
            try:
                self.delete(f"/session/{self.sid}")
            except Exception:
                pass
            self.sid = None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_geckodriver() -> str:
    if os.environ.get("KAGI_GECKODRIVER"):
        return os.environ["KAGI_GECKODRIVER"]
    p = shutil.which("geckodriver")
    if p:
        return p
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "geckodriver.exe")
    if os.path.exists(here):
        return here
    raise SystemExit(
        "geckodriver not found.\n"
        "  - Vendored copy is bundled at bin/geckodriver.exe\n"
        "  - Or install it and set KAGI_GECKODRIVER / put geckodriver on PATH.\n"
        "    Download: https://github.com/mozilla/geckodriver/releases"
    )


def find_librewolf() -> str:
    if os.environ.get("KAGI_LIBREWOLF"):
        return os.environ["KAGI_LIBREWOLF"]
    p = shutil.which("librewolf")
    if p:
        return p
    plat = sys.platform
    if plat in LIBREWOLF_DEFAULTS and os.path.exists(LIBREWOLF_DEFAULTS[plat]):
        return LIBREWOLF_DEFAULTS[plat]
    candidates = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        for c in ("LibreWolf", "Programs/librewolf"):
            if os.path.exists(os.path.join(la, c, "librewolf.exe")):
                candidates.append(os.path.join(la, c, "librewolf.exe"))
    for c in candidates:
        if os.path.exists(c):
            return c
    raise SystemExit(
        "Could not find the LibreWolf browser.\n"
        "  - Set KAGI_LIBREWOLF to the path of the LibreWolf binary."
    )


def profile_candidates() -> list:
    """Return LibreWolf profile dirs in priority order (most likely first)."""
    if os.environ.get("KAGI_PROFILE"):
        return [os.environ["KAGI_PROFILE"]]
    appdata = os.environ.get("APPDATA", "")
    ini = os.path.join(appdata, "librewolf", "profiles.ini")
    base = os.path.join(appdata, "librewolf", "Profiles")
    dirs = {}      # path -> is_locked
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

    ordered = []
    locked = [p for p, lk in dirs.items() if lk]
    for p in locked + [d for d in dirs if d not in locked]:
        if default_mark and p == default_mark:
            continue  # pushed to front below
        ordered.append(os.path.join(base, p))
    if default_mark:
        # marked default goes first if it exists
        ordered.insert(0, os.path.join(base, default_mark))
    # de-dup, keep existing dirs
    seen, out = set(), []
    for d in ordered:
        if d in seen:
            continue
        seen.add(d)
        if os.path.isdir(d):
            out.append(d)
    return out


def cookies_db_with_session(profiles: list) -> str:
    """Return path to a cookies.sqlite holding a valid kagi_session, or ''."""
    for profile in profiles:
        db = os.path.join(profile, "cookies.sqlite")
        if not os.path.exists(db):
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            try:
                cur = con.execute(
                    "SELECT length(value) FROM moz_cookies WHERE host LIKE '%kagi.com' "
                    "AND name='kagi_session' LIMIT 1"
                )
                row = cur.fetchone()
                if row and row[0] and row[0] > 20:
                    return db
            finally:
                con.close()
        except Exception:
            continue
    return ""


def load_kagi_cookies(db: str):
    import browser_cookie3 as bc
    return list(bc.librewolf(cookie_file=db, domain_name=COOKIE_DOMAIN))


RESULT_JS = r"""
var out = {qa: '', results: []};
var qa = document.querySelector('._0_qa_reply_box');
if (qa) out.qa = (qa.textContent || '').replace(/\s+/g, ' ').trim();
document.querySelectorAll('div._0_SRI.search-result').forEach(function(r){
  var t = r.querySelector('.__sri_title_link');
  var sn = r.querySelector('.__sri-desc');
  var title = (t ? (t.getAttribute('title') || t.textContent) : '').trim();
  var snippet = '';
  if (sn) {
    var cl = sn.cloneNode(true);
    cl.querySelectorAll('a,button,script,style').forEach(function(e){e.remove();});
    snippet = (cl.textContent || '').replace(/\s+/g, ' ').trim();
  }
  out.results.push({
    title: title,
    url: (t && t.href) || '',
    snippet: snippet
  });
});
out.related = Array.from(document.querySelectorAll('.related-searches a')).map(function(a){return (a.textContent||'').trim();});
return JSON.stringify(out);
"""


def run_search(query: str, limit: int, lens: str, with_related: bool) -> dict:
    db = cookies_db_with_session(profile_candidates())
    if not db:
        raise SystemExit(
            "No LibreWolf profile with a Kagi login found.\n"
            "  - Log in at https://kagi.com in LibreWolf first, then retry.\n"
            "  - Or point KAGI_PROFILE at the profile directory holding your Kagi cookies."
        )
    cookies = load_kagi_cookies(db)
    if not cookies:
        raise SystemExit("No Kagi cookies read from profile.")

    gecko = find_geckodriver()
    binary = find_librewolf()
    port = free_port()

    gkm = subprocess.Popen(
        [gecko, "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    wd = WDClient(port)
    try:
        time.sleep(1.5)
        wd.new_session(binary, [])
        # 1) land on kagi.com so cookies can be set for that domain
        wd.navigate("https://kagi.com/")
        time.sleep(4)
        for c in cookies:
            if c.name == "translate_custom_css_enabled":
                continue
            wd.add_cookie(c.name, c.value, c.domain, c.path,
                          bool(c.secure), True)
        # 2) run the search
        params = {"q": query}
        if lens:
            params["lens"] = lens
        url = "https://kagi.com/search?" + urllib.parse.urlencode(params)
        wd.navigate(url)
        time.sleep(8)
        raw = wd.exec_js("return document.title;")
        if "Sign In" in (raw or "") and "Kagi" not in (raw or ""):
            raise SystemExit("Kagi redirected to Sign In — your session cookie may be expired. Log in again in LibreWolf.")
        data = json.loads(wd.exec_js(RESULT_JS))
    finally:
        wd.close()
        gkm.terminate()
        try:
            gkm.wait(timeout=5)
        except Exception:
            gkm.kill()

    data["results"] = (data.get("results") or [])[:limit]
    if not with_related:
        data.pop("related", None)
    data["query"] = query
    return data


def render_markdown(data: dict, with_summary: bool) -> str:
    lines = []
    qa = data.get("qa") or ""
    if qa and with_summary:
        lines.append("## Quick Answer")
        lines.append(qa)
        lines.append("")
    for i, r in enumerate(data.get("results") or [], 1):
        lines.append(f"--- Result {i} ---")
        lines.append(f"Title: {r['title']}")
        lines.append(f"Link: {r['url']}")
        if r.get("snippet"):
            lines.append(f"Snippet: {r['snippet']}")
        lines.append("")
    related = [x for x in (data.get("related") or []) if x]
    if related:
        lines.append("## Related searches")
        for r in related:
            lines.append(f"- {r}")
    return "\n".join(lines).rstrip()


def main():
    ap = argparse.ArgumentParser(description="Kagi web search using your LibreWolf login cookie (no API key).")
    ap.add_argument("query", nargs="?", help="search query")
    ap.add_argument("-n", "--limit", type=int, default=10, help="max results (default 10)")
    ap.add_argument("--lens", default="", help="Kagi lens, e.g. code, videos, news, forums, academic")
    ap.add_argument("--offset", type=int, default=0, help="unused placeholder for compatibility")
    ap.add_argument("--json", action="store_true", help="output JSON")
    ap.add_argument("--no-related", action="store_true", help="hide related searches")
    ap.add_argument("--no-quick", action="store_true", help="omit the quick answer block")
    ap.add_argument("--summary", action="store_true", help="include the Kagi quick answer as a summary")
    args = ap.parse_args()

    if not args.query:
        ap.print_help()
        sys.exit(1)

    data = run_search(args.query, args.limit, args.lens, not args.no_related)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(data, with_summary=args.summary or not args.no_quick))


if __name__ == "__main__":
    main()