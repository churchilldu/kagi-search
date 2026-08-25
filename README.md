# kagi-search

Search Kagi using your logged-in LibreWolf session cookie — pure HTTP +
SSE, stdlib only. No Kagi API key, no API subscription, no headless browser.

```bash
python search.py "pi coding agent" -n 5
python search.py "consensus algorithms" --lens programming --json
python search.py "web frameworks 2025" --offset 10
```

## How it works
Reads `kagi_session` from LibreWolf's `cookies.sqlite` and replays the same two
requests the Kagi web app makes:
1. `GET /search?q=...` — warms cookies / validates the session.
2. `GET /socket/search?q=...&nonce=...` — Server-Sent Events → results JSON.

Reverse-engineered from an HAR capture of a manual search; verified live. No
dependencies beyond the Python standard library.

## Layout
- `search.py` — the CLI (cookie → HTTP → SSE → results)
- `SKILL.md` — the pi skill manifest/instructions
- `references/implementation.md` — technical notes & SSE schema

## Requirements
- LibreWolf (logged into Kagi)
- Python 3.8+

## Env overrides
`KAGI_PROFILE` — profile dir holding `cookies.sqlite` (auto-detected otherwise).

## License
MIT.
