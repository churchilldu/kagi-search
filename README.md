# kagi-search

Search Kagi using your logged-in browser session cookie — pure HTTP + SSE. No
Kagi API key, no API subscription, no headless browser, no WebDriver.

```bash
python search.py "pi coding agent" -n 5
python search.py "consensus algorithms" --lens programming --json
python search.py "web frameworks 2025" --offset 10
```

## How it works
Reads the `kagi_session` cookie from whichever browser you are logged into Kagi
with (via `browser_cookie3`: LibreWolf, Firefox, Chrome, Edge, Brave, Chromium,
Vivaldi, Opera, Safari) and replays the same two requests the Kagi web app
makes:
1. `GET /search?q=...` — warms cookies / validates the session.
2. `GET /socket/search?q=...&nonce=...` — Server-Sent Events → results JSON.

Reverse-engineered from an HAR capture of a manual search; verified live. Cookie
decryption/DB-copying quirks of each browser are the library's problem, so the
tool is portable across platforms and browsers.

## Layout
- `search.py` — the CLI (cookie → HTTP → SSE → results)
- `SKILL.md` — the pi skill manifest/instructions
- `references/implementation.md` — technical notes & SSE schema

## Requirements
- Any supported browser, logged into Kagi
- Python 3.8+, `browser-cookie3` (`pip install --user -r requirements.txt`)

## Env overrides
`KAGI_BROWSER` — read cookies from this browser only, e.g. `chrome`.
`KAGI_PROFILE` — explicit Firefox/LibreWolf profile dir (when auto-detect fails).
`KAGI_CACHE_FILE` — cookie cache path (default `.kagi_cache.json`).

## License
MIT.
