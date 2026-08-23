# Implementation notes

## Why cookie interception + real browser (not the API)

- **Kagi's paid Search API** requires a subscriber API token (`KAGI_API_KEY`)
  and is currently in beta. This skill deliberately avoids it by reusing the
  user's logged-in browser session.
- **Plain HTTP scraping fails.** Testing showed that `GET /search?q=...` with
  the `kagi_session` cookie via urllib/httpx returns only Kagi's app shell —
  ~23 anchors, no organic results. Kagi renders organic results for real
  browser requests and serves an empty shell to non-browser requests (client
  render + anti-bot). There is **no** separate results JSON endpoint in
  `k_serp.js`/`k_sea.js`; the DOM is server-rendered only when the request
  looks like a real browser.
- **Solution:** drive real (headless) LibreWolf with the user's cookies via
  geckodriver → full results every time.

## Cookie storage in LibreWolf

- Profile: `%APPDATA%\librewolf\Profiles\<profile>\cookies.sqlite`
- Active profile found via `%APPDATA%\librewolf\profiles.ini` (prefers the
  `[Install...] Locked=1` default, else `Default=1`).
- Kagi cookies (table `moz_cookies`):
  - `.kagi.com` → `kagi_session` (auth; ~55 chars)
  - `kagi.com` → `_kagi_search_`, `kagi_previous_page`
- Values are stored in plaintext (no key4 decryption needed for cookies).
- Read-only access via `sqlite3.connect("file:...?mode=ro")`; the profile is
  never modified even if LibreWolf is running.

## Headless flow (search.py)

1. Locate profile → pick `cookies.sqlite` containing a `kagi_session`.
2. `browser_cookie3.librewolf(cookie_file=..., domain_name='kagi.com')` → jar.
3. Start geckodriver on a free port; POST `/session` with
   `moz:firefoxOptions.binary` = LibreWolf and `args:["-headless"]`.
4. **Navigate to `https://kagi.com/` first** — WebDriver refuses cookie
   injection while the document is `about:blank` ("Document is cookie-averse").
5. `POST /session/{id}/cookie` for each kagi cookie (body must be wrapped as
   `{"cookie": {...}}`).
6. `POST /session/{id}/url` to `https://kagi.com/search?q=...&lens=...`.
7. `GET /session/{id}/execute/sync` runs extraction JS returning JSON.
8. Tear down: `DELETE /session/{id}`, terminate geckodriver.

## Result DOM (subject to Kagi's markup changes)

- Result item: `div._0_SRI.search-result`
- Title/href: `a.__sri_title_link` (prefer its `title` attribute for the full
  title; falls back to text)
- Snippet: `div.__sri-desc` (text, collapsed to single spaces)
- Quick answer (AI free summary): `div._0_qa_reply_box`
- Related searches: `.related-searches a`

Keep `RESULT_JS` in `search.py` as the single source of truth for selectors so
markup changes are easy to patch.

## Known limits / future work

- Results are capped to what fits on Kagi page 1 (typical ~15). Pagination is
  a JS-SPA feature with no stable URL param; not yet wired in. `--offset` is a
  compatibility placeholder.
- `geckodriver` bundled here is Windows x64 — ship per-platform binary or rely
  on PATH.
- First run of a fresh headless profile may be a bit slower (~10–15s) due to
  browser bootstrap + JS render; an 8s render settle is currently hardcoded.