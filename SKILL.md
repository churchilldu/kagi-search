---
name: kagi-search
description: >-
  Web search and related-query discovery via Kagi by reusing the logged-in
  LibreWolf session cookie (scraped from the local browser profile) and rendering
  results in a real headless LibreWolf. No Kagi API key and no subscription
  paywall for the API. Use when the user asks to search the web, wants current
  information/facts/docs, or wants better personalization/quality than Brave.
  Returns title, link, snippet and related searches; also surfaces Kagi's AI
  "quick answer" as a free summary.
---

# Kagi Search (via LibreWolf session cookie)

Search Kagi with your **existing logged-in LibreWolf profile** — no Kagi API key.
The script reads the `kagi_session` cookie from LibreWolf's `cookies.sqlite`,
injects it into a real headless LibreWolf instance (via geckodriver), runs the
search, and extracts the rendered results.

> Why this works: Kagi serves full organic results only to real browser
> requests. A plain HTTP `requests`/`httpx` call with the cookie returns an
> empty app shell (client-side render + anti-bot). Driving the real LibreWolf
> engine with your real cookies keeps the fingerprint and session authentic, so
> you get full results with your personalization — no API key.

## Requirements

- LibreWolf installed (the default location is auto-detected; override with `KAGI_LIBREWOLF`).
- A Kagi account **logged in** in LibreWolf.
- Python 3.8+ and Google Chrome-independent:
  - `pip install -r requirements.txt` (adds `browser_cookie3` only).
  - `geckodriver` — bundled at `bin/geckodriver.exe` (win64). For other OSes,
    download from https://github.com/mozilla/geckodriver/releases and put it on
    PATH or set `KAGI_GECKODRIVER`.

## Usage

```bash
python search.py "your query"                 # top 10 results
python search.py "query" -n 5                  # fewer results
python search.py "query" --lens code           # Kagi lens (code, videos, news, forums, ...)
python search.py "query" --json                # machine-readable output
python search.py "query" --no-quick            # omit AI quick answer
python search.py "query" --no-related          # hide related searches
```

From pi, invoke it through this skill (the agent runs `search.py` for you).

## Environment overrides

| Variable | Meaning |
|----------|---------|
| `KAGI_LIBREWOLF` | Path to the LibreWolf executable |
| `KAGI_GECKODRIVER` | Path to geckodriver |
| `KAGI_PROFILE` | Path to the LibreWolf profile dir holding `cookies.sqlite` |

## Output format

```
--- Result 1 ---
Title: actions/setup-python: Set up your ...
Link: https://github.com/actions/setup-python
Snippet: Migrated action internals to ESM ...

## Related searches
- github actions python setup
- github actions python versions
```

## When to use

- Searching for documentation, facts, or current information.
- When you want Kagi-quality results and personalization without an API key.
- Any web-research task; combine with the `zotero` skill for citation workflows.

## Notes & troubleshooting

- If you get a **"Sign In"** error, your Kagi session expired — log in at
  `https://kagi.com` in LibreWolf and retry.
- Result count reflects what fits on Kagi page 1; pass `-n` to trim.
- Everything runs **headless** and is torn down after each query; your profile
  is read-only and never modified.