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

## Cookie source

- LibreWolf stores cookies in `%APPDATA%\librewolf\Profiles\<profile>\cookies.sqlite`.
- Active profile resolved from `%APPDATA%\librewolf\profiles.ini` (prefers the
  `[Install...] Locked=1` default, else `Default=1`); scan candidates for one
  that actually holds a `kagi_session`.
- Read via `sqlite3.connect("file:...?mode=ro")` — profile is never modified,
  even if LibreWolf is running.
- Cookies are plaintext; no key4 decryption needed for cookie values.

## Why not the earlier headless-browser version?

An earlier version drove headless LibreWolf via geckodriver to let Kagi render.
It worked but added a ~4 MB binary + `browser_cookie3` dependency. The SSE
endpoint makes pure HTTP possible, which is faster, lighter, and stdlib-only.
Reach for JSON-mode (`--json`) if you need the raw item objects downstream.

## Payload fragility warning

Kagi's markup/tags can change. Selectors and SSE tag names and item keys are
centralized in `search.py` (`parse_sse`, `run_search`) so a future shim is a
single-point fix. If the API-hostile `socket/search` endpoint ever disappears,
re-adding the headless-LibreWolf renderer (`references/` has the WebDriver
recipe) is the fallback.