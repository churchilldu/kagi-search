# kagi-search (pi skill)

Search **Kagi** using your **logged-in LibreWolf session cookie** — rendered in a
real headless LibreWolf and extracted. No Kagi API key, no API subscription.

See [`SKILL.md`](SKILL.md) for usage, or just run:

```bash
pip install -r requirements.txt
python search.py "pi coding agent" -n 5
python search.py "github actions setup-python" --json
python search.py "web frameworks 2025" --lens code
```

## How it works
1. Reads `kagi_session` (+ friends) from LibreWolf's `cookies.sqlite`.
2. Launches **headless LibreWolf** via `geckodriver` and injects those cookies.
3. Runs the search, waits for render, extracts title / URL / snippet / related
   searches (and the free AI quick answer).

A plain HTTP request with the cookie returns an empty shell, so this drives the
real browser to get full results.

## Layout
- `search.py` — the CLI (cookie → headless browser → results)
- `bin/geckodriver.exe` — vendored geckodriver (Windows x64)
- `SKILL.md` — the pi skill manifest/instructions
- `references/implementation.md` — deep technical notes

## Requirements
- LibreWolf (logged into Kagi)
- Python 3.8+ and `pip install -r requirements.txt`

## Env overrides
`KAGI_LIBREWOLF`, `KAGI_GECKODRIVER`, `KAGI_PROFILE` — see SKILL.md.

## License
MIT (MIT-style; see LICENSE).