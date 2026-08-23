---
name: kagi-search
description: >-
  Web search via Kagi.
  Use when the user asks to search the web, wants current information/facts/docs,
  or wants better personalization/quality than Brave.
  Returns title, link, snippet, source, rank and related searches;
  also surfaces Kagi's AI "quick answer".
---

# Kagi Search (via LibreWolf session cookie)

Search Kagi with your **existing logged-in LibreWolf profile** — no Kagi API key,
no headless browser, no HTTP library. It reads the Kagi session cookie from
LibreWolf's `cookies.sqlite` and replays the two requests the web app itself
makes:

1. `GET /search?q=...` — warms up fresh cookies / validates the session.
2. `GET /socket/search?q=...&nonce=...` — a Server-Sent Events stream carrying
   structured results JSON (`search_results_json`), the answer box, related
   searches, and the AI quick answer.

Yes, it is scraper-flavoured (session reuse instead of the paid API), but it
delivers full organic results with your personalization — zero dependencies.

## Requirements

- LibreWolf installed, **logged in** to Kagi.
- Python 3.8+. **No third-party packages** (uses `sqlite3` + `urllib` only).

## Usage

```bash
python search.py "your query"                  # top 10 results
python search.py "query" -n 5                   # fewer results
python search.py "query" --offset 5             # skip first 5 (pagination)
python search.py "query" --lens code            # Kagi lens (code, videos, news, forums, academic, ...)
python search.py "query" --json                 # machine-readable output
python search.py "query" --no-quick             # omit AI quick answer
python search.py "query" --no-related           # hide related searches
```

From pi, invoke it through this skill (the agent runs `search.py` for you).

## Environment overrides

| Variable | Meaning |
|----------|---------|
| `KAGI_PROFILE` | Path to the LibreWolf profile dir holding `cookies.sqlite` |

## Output format

```
--- Result 1 ---
Title: Pi Coding Agent
Link: https://pi.dev/
Snippet: A terminal-based coding agent Pi is a minimal agent harness. ...
Source: pi.dev

## Related searches
- pi coding agent github
- pi coding agent extensions
```

## When to use

- Searching for documentation, facts, or current information.
- When you want Kagi-quality results and personalization without an API key.
- Any web-research task; combine with the `zotero` skill for citation workflows.

## Notes & troubleshooting

- If you get a **"Sign In"** / "session rejected" error, your Kagi session
  expired — log in at `https://kagi.com` in LibreWolf and retry.
- Your profile is **read-only**: reads `cookies.sqlite` (mode=ro) and never
  modifies it, even while LibreWolf is running.
- Returns up to ~60 results per query (fit on Kagi's page 1); trim with `-n` or
  paginate with `--offset`.
