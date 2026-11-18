# Implementation notes

## How the no-API, no-browser approach works

Kagi's web app loads results **client-side**. The `/search?q=...` HTML response
is just an app shell; the actual results arrive over a **Server-Sent Events
(SSE)** stream from:

```
GET https://kagi.com/socket/search?q=<query>&nonce=<32-hex>
```

Reverse-engineered from a HAR capture of a manual LibreWolf search. Two plain
requests are enough — no headless browser, no geckodriver, no API key.

## Request 1 — warm up + validate

```
GET https://kagi.com/search?q=...&lens=...
Cookie: _kagi_search_=<old>; kagi_session=<session>
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) Gecko/20100101 Firefox/153.0
Accept: text/html,...
Referer: https://kagi.com/
Sec-Fetch-*: document / navigate / same-origin
```
- Hard requirement: a **real Firefox/LibreWolf User-Agent** — a generic
  `python-requests` UA returns the empty shell instead of a valid session.
- Response sets fresh `kagi_session` / `_kagi_search_` cookies (parse the
  `Set-Cookie` headers into a fresh cookie string for request 2).
- A redirect to `/signin` means the session expired → tell the user to log in.

## Request 2 — the results stream

```
GET https://kagi.com/socket/search?q=...&lens=...&nonce=614e...12ab
Cookie: _kagi_search_=<fresh>; kagi_session=<session>
X-Kagi-Authorization: <session>          # = kagi_session value (also sent as header!)
Accept: text/event-stream
Referer: https://kagi.com/search?q=...
Sec-Fetch-*: empty / cors / same-origin
Cache-Control: no-cache
```
- `X-Kagi-Authorization` **equals the `kagi_session` cookie value** — this is the
  auth token for the API-style socket endpoint.
- `nonce` is a fresh random 32-hex `secrets.token_hex(16)`.
- Response is SSE: `data: [{"tag":"...","payload":"..."}]` blocks separated by
  blank lines, ending with `id: CLOSE`.

## SSE tags (payload is a JSON array of `{tag,payload}`)

| tag | payload | use |
|-----|---------|-----|
| `search_results_json` | JSON string: `{"items":[{title,url,snippet,source,rank}], "answer_box":...}` | **the results** |
| `top_content` | HTML fragment (may be a JSON/dict for some lenses) | optional AI "quick answer" in `_0_qa_reply_box` |
| `related_searches` | HTML `<div class="related-searches">` with `<a href="?q=..."><span>text</span>` | related query suggestions |
| `domain_info`, `search` | misc | not currently used |

Returns up to ~60 items per stream (page-1 volume), so `-n`/`--offset` slice
locally — pagination needs no extra request.

## Cookie source + caching

- Cookies come from `browser_cookie3`, so the tool works with whatever browser
  you are logged into Kagi with: LibreWolf, Firefox, Chrome, Edge, Brave,
  Chromium, Vivaldi, Opera, Safari. Order is `BROWSER_ORDER` in `search.py`
  (LibreWolf/Firefox first: Kagi cookies are plaintext there, no key
  decryption), and the first browser holding a `kagi_session` wins.
- Browsers are probed with `domain_name="kagi.com"`; results are filtered to
  `kagi_session`, `_kagi_search_`, `kagi_previous_page`.
- The library handles the platform-specific mess: copying the cookie DB out
  from under a running browser, VSS shadow copies for locked Chrome DBs,
  DPAPI/`Local State` key decryption for Chromium v80+, and recovery of
  session cookies from `sessionstore` for Firefox-based browsers. The profile
  is never modified, so it works while the browser is open.
- `KAGI_BROWSER` pins one browser; `KAGI_PROFILE` supplies an explicit
  Firefox/LibreWolf profile dir (passed as `cookie_file`).
- When nothing yields a session, the error lists every browser tried with the
  failure reason (e.g. `chrome: BrowserCookieError: Unable to get key for
  cookie decryption`) so decryption problems are diagnosable.

### Cache (`<skill dir>/.kagi_cache.json`)

The Kagi cookies (`kagi_session`, `_kagi_search_`, `kagi_previous_page`) are
cached to a JSON file in the skill dir to avoid reading browser stores every
run:

- `read_cookie_cache()` returns the cached `{name:value}` map only if
  `expires_at` > now (or the file does not exist) — else `{}`.
- `load_cookies(use_cache)` → cache hit, or fall back to
  `read_browser_cookies()` and `write_cookie_cache()`.
- `expires_at` comes from the cookie's own expiry via `_expiry_seconds()`,
  which normalizes Firefox's millisecond `moz_cookies.expiry` values; when the
  cookie has no real expiry (session cookie), falls back to
  `DEFAULT_TTL_SECONDS` (6 h).
- The warm-up `/search` response's fresh `Set-Cookie` values are written back
  to the cache each run, so the cached session stays current.
- `--no-cache` bypasses the cache (force a browser read). Cache writes are
  best-effort and atomic (`os.replace` of a `.tmp`), and the file is
  git-ignored because it holds a session secret. Override the path with
  `KAGI_CACHE_FILE`.

## Why not the earlier headless-browser version?

An earlier version drove headless LibreWolf via geckodriver to let Kagi render.
It worked but added a ~4 MB browser binary plus WebDriver plumbing. The SSE
endpoint makes pure HTTP possible, which is faster, lighter, and needs only
`browser_cookie3` for the cookie hand-off. Reach for JSON-mode (`--json`) if
you need the raw item objects downstream.

## Payload fragility warning

Kagi's markup/tags can change. Selectors and SSE tag names and item keys are
centralized in `search.py` (`parse_sse`, `run_search`) so a future shim is a
single-point fix. If the API-hostile `socket/search` endpoint ever disappears,
re-adding the headless-LibreWolf renderer (`references/` has the WebDriver
recipe) is the fallback.