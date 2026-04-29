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

### Core (URL collector)

| Variable | Default | Purpose |
| --- | --- | --- |
| `JOBRIGHT_EMAIL` | _required_ | Email used to sign in when not auto-redirected. |
| `JOBRIGHT_PASSWORD` | _required_ | Password for sign-in. |
| `POST_LOAD_WAIT_SECONDS` | `3` | Wait after initial page load before checking for redirect. |
| `REDIRECT_WAIT_SECONDS` | `15` | Max wait for the post-login redirect to `/jobs/recommend`. |
| `APPLIED_URLS_FILE` | `applied_jobs.jsonl` | Output JSONL file. |
| `BROWSER_PROFILE_DIR` | `.pw_profile` | Persistent Chromium user-data dir. |
| `ACTION_TIMEOUT_MS` | `15000` | Default per-action timeout (ms). |

### Autobid / autofill profile

| Variable | Purpose |
| --- | --- |
| `APPLICANT_FIRST_NAME` / `APPLICANT_LAST_NAME` | Standard name fields. |
| `APPLICANT_PREFERRED_NAME` | Optional preferred first name. |
| `APPLICANT_EMAIL` | Email for the application. |
| `APPLICANT_PHONE` | Phone (E.164 recommended, e.g. `+15551234567`). |
| `APPLICANT_COUNTRY` | Country label as it appears in Greenhouse's dropdown. |
| `APPLICANT_LINKEDIN` / `APPLICANT_WEBSITE` / `APPLICANT_GITHUB` | URLs. |
| `APPLICANT_CITY` / `APPLICANT_STATE` / `APPLICANT_POSTAL_CODE` | Address. |
| `APPLICANT_RESUME_PATH` | Path (relative or absolute) to your resume PDF. |
| `APPLICANT_GENDER` / `APPLICANT_HISPANIC_ETHNICITY` / `APPLICANT_RACE` / `APPLICANT_VETERAN_STATUS` / `APPLICANT_DISABILITY_STATUS` | Voluntary self-identification (must match the option label exactly). |
| `APPLICANT_SUMMARY` | Free-text candidate summary used by the AI prompt. |

### AI for unknown questions

| Variable | Default | Purpose |
| --- | --- | --- |
| `AI_PROVIDER` | _empty_ | Set to `openai` to enable AI answers; leave blank to skip. |
| `OPENAI_API_KEY` | _empty_ | Your OpenAI API key. |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model used for question answering. |
| `AI_PROMPT_FILE` | `ai_prompt.txt` | Path to the editable prompt template. Edit this file to change how AI answers are written. |

## Autobid (Greenhouse autofill)

The autobid module fills Greenhouse application forms (`job-boards.greenhouse.io`)
from your `.env` profile. Standard fields (name, email, phone, dropdowns)
are filled deterministically; any free-form question is answered by the
AI using the prompt template at `ai_prompt.txt` (which you can edit).

### One-time setup

1. Fill in the `APPLICANT_*` section of your `.env`.
2. Drop your resume PDF somewhere reachable and set `APPLICANT_RESUME_PATH`.
3. (Optional) Set `AI_PROVIDER=openai` and `OPENAI_API_KEY` for AI answers
   to unknown free-form questions.
4. (Optional) Edit `ai_prompt.txt` to tweak how the AI writes answers.

### Test the autobid

The test CLI walks URLs from `applied_jobs.jsonl`, filters to Greenhouse
ones, fills each form, and (by default) holds the tab open for review:

```powershell
# Fill the FIRST greenhouse application from applied_jobs.jsonl, hold 30s for review
python -m greezik.test_autobid --limit 1 --hold 30

# Fill an arbitrary URL
python -m greezik.test_autobid --url "https://job-boards.greenhouse.io/embed/job_app?for=...&token=..."

# DANGER -- actually submit one application
python -m greezik.test_autobid --limit 1 --submit
```

Available flags:

- `--source PATH` — JSONL source file (default `applied_jobs.jsonl`).
- `--url URL` — process this URL instead of the JSONL (repeatable).
- `--limit N` — at most N URLs.
- `--submit` — actually submit the application (default is **dry run**).
- `--hold SECONDS` — keep the tab open this long after fill (dry-run only).
- `--headless` — run without a visible browser window.
- `--verbose` — DEBUG-level logs.

The autobid runner uses a fresh non-persistent Chromium context so it
won't pollute the main bot's `.pw_profile/`.

## Resetting the browser profile

If sign-in gets stuck or you want a clean session, delete the profile:

```powershell
Remove-Item -Recurse -Force .pw_profile
```

## Exit codes

- `0` — clean stop (no more apply buttons).
- `2` — login failed (could not reach `/jobs/recommend`).
- `3` — unhandled error (see `logs/greezik.log`).
