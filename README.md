# Greezik

A Python + Playwright bot that signs into [jobright.ai](https://jobright.ai/),
walks the recommended jobs feed, clicks through each Apply button (handling both
the direct external-redirect case and the "Apply without Customizing" modal
case), saves the resulting external application URL locally, and confirms each
application via the "Yes, I applied!" popup.

This first iteration only **collects external apply URLs**; the auto-bid /
auto-fill of the external job site will be implemented later.

## Requirements

- Python 3.11+
- Windows PowerShell, macOS, or Linux
- Roughly 400 MB free for the Chromium download

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env
# edit .env and set JOBRIGHT_EMAIL / JOBRIGHT_PASSWORD
```

On macOS / Linux replace the activate and copy commands accordingly:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

## Run

```powershell
python -m greezik.main
```

Or use the convenience script on Windows:

```powershell
.\run.ps1
```

The bot will:

1. Launch a persistent headful Chromium profile (default `./.pw_profile`).
2. Open `https://jobright.ai/` and wait `POST_LOAD_WAIT_SECONDS` seconds.
3. If it lands on `/jobs/recommend`, skip sign-in. Otherwise click `SIGN IN`,
   fill credentials from `.env`, and submit.
4. Click the first visible Apply button. If a new tab opens, save its URL.
   If a modal opens instead, click "Apply without Customizing" and save the
   URL of the new tab that follows.
5. Close the external tab, click "Yes, I applied!" on the recommend page,
   and repeat until no more Apply buttons are visible (after a final scroll).

Applied URLs are appended to `APPLIED_URLS_FILE` (default
`applied_jobs.jsonl`) as JSON Lines, e.g.:

```json
{"applied_at": "2026-04-29T07:52:13Z", "external_url": "https://boards.greenhouse.io/...", "source": "jobright.ai/jobs/recommend"}
```

## Configuration (`.env`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `JOBRIGHT_EMAIL` | _required_ | Email used to sign in when not auto-redirected. |
| `JOBRIGHT_PASSWORD` | _required_ | Password for sign-in. |
| `POST_LOAD_WAIT_SECONDS` | `3` | Wait after initial page load before checking for redirect. |
| `REDIRECT_WAIT_SECONDS` | `15` | Max wait for the post-login redirect to `/jobs/recommend`. |
| `APPLIED_URLS_FILE` | `applied_jobs.jsonl` | Output JSONL file. |
| `BROWSER_PROFILE_DIR` | `.pw_profile` | Persistent Chromium user-data dir. |
| `ACTION_TIMEOUT_MS` | `15000` | Default per-action timeout (ms). |

## Resetting the browser profile

If sign-in gets stuck or you want a clean session, delete the profile:

```powershell
Remove-Item -Recurse -Force .pw_profile
```

## Exit codes

- `0` — clean stop (no more apply buttons).
- `2` — login failed (could not reach `/jobs/recommend`).
- `3` — unhandled error (see `logs/greezik.log`).
