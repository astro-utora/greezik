# Greezik

> A Playwright bot that does the soul-crushing part of the job hunt for you.
> You provide the resumes and the coffee. Greezik provides the clicks. ☕🤖

Greezik signs into [jobright.ai](https://jobright.ai/), walks the
**Recommended Jobs** feed, decides whether each role is worth your time,
auto-fills the external Greenhouse application with the **right** resume
for *that* job, answers any free-form questions with an AI that has
actually read your resume, and confirms the "Yes, I applied!" popup —
then does it again, and again, and again, until you run out of jobs or
the universe runs out of patience. 🌀

It also has a **backfill** mode for when you've been applying by hand
like a peasant and want to bulk-import your `/jobs/applied` history into
the same JSONL log the bot uses. 📜

---

## ✨ Features

- **End-to-end auto-apply** on `jobright.ai/jobs/recommend`: open the
  card, scrape the JD, route the Apply click, fill the form, submit,
  confirm, repeat.
- **JD judge** (`steps/step0_judge_jd.py`) decides BID vs. SKIP using a
  three-layer pipeline: title blacklist, hard disqualifiers, and a
  weighted fit score against your resume vocabulary. No "Senior
  Director of Synergy" applications. 🙅
- **Per-job resume matcher** (`steps/step1_match_resume.py`) scores
  every `.docx` in your resumes folder against the JD and uploads the
  paired `.pdf` of the best match. Bring your own variants — Greezik
  picks the right one. 🎯
- **Greenhouse autobid** fills standard fields deterministically
  (name, email, phone, address, EEO dropdowns, education) and routes
  any free-form question to OpenAI with a prompt that's grounded in
  the candidate resume + JD + company summary.
- **Gmail IMAP** integration for the "verify your email" one-time code
  that some Greenhouse boards send before they'll accept the
  submission. Pulls the code, types it in, optionally deletes the
  email afterwards. 📬
- **Per-company dedup window** so you don't spam the same employer 4
  times in 2 days. Configurable; recently-applied companies get
  routed to the skipped log instead.
- **Applied log auto-pruning** so `big_log.jsonl` doesn't slowly
  consume the disk over months of unattended running.
- **Manual-required mode** for overnight unattended runs: failed
  Greenhouse submits get logged to `manual_required.jsonl` instead of
  popping a dialog you'll never see at 3am. 🌙
- **Backfill subcommand** that walks `/jobs/applied` top-to-bottom and
  appends the recent slice to `big_log.jsonl`, stopping at the first
  card older than your configured threshold (default 1 week).
- **Persistent or fresh Chromium profiles**, optional proxy support,
  optional headless mode, optional everything really.

---

## 📋 Requirements

- **Python 3.11+**
- Windows / macOS / Linux (PowerShell, bash, zsh, anything that runs
  Python and gets out of the way)
- ~400 MB of disk for the Chromium download Playwright pulls in
- A Jobright account that you'd like the bot to drive
- *(Optional but recommended)* An OpenAI API key for the free-form
  question answers
- *(Optional)* A Gmail account + app password if you want the bot to
  handle Greenhouse's email verification step automatically

---

## 🛠️ Setup

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env
# now open .env in your editor of choice and fill it in
```

### macOS / Linux

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

At a bare minimum you need to set:

- `JOBRIGHT_EMAIL` and `JOBRIGHT_PASSWORD`
- `APPLICANT_*` identity fields (name, email, phone, address, etc.)
- `APPLICANT_RESUMES_DIR` pointed at a folder of paired `.docx` + `.pdf`
  resume variants — same stem, e.g. `tyler_python_aws.docx` next to
  `tyler_python_aws.pdf`. Without this, every job is recorded as
  skipped, which is not the vibe. 📄

---

## 🚀 Run

### Auto-apply (the main event)

```powershell
python -m greezik.main
# or, equivalently:
python -m greezik.main apply
```

Or, on Windows, the convenience launcher:

```powershell
.\run.ps1
```

### Backfill `/jobs/applied` into the log

```powershell
python -m greezik.main backfill
```

This walks `/jobs/applied` from the top, appends one record per card
to `APPLIED_URLS_FILE`, clicks each card's **X / remove** button so the
list keeps moving, and stops as soon as a card's publish time is older
than `BACKFILL_STOP_AFTER` (default `1w`).

### What the apply loop actually does

1. Launches Chromium with the persistent profile at `BROWSER_PROFILE_DIR`.
2. Opens `https://jobright.ai/` and waits `POST_LOAD_WAIT_SECONDS`.
3. If already on `/jobs/recommend`, skips sign-in. Otherwise clicks
   **SIGN IN**, fills credentials from `.env`, submits.
4. Clicks the first job title to open the detail panel, scrapes the
   JD into `bid/jobright/Job_Description.txt`, runs the **judge** to
   decide BID or SKIP, and runs the **resume matcher** to pick the
   best `.pdf` for this JD.
5. Clicks **Apply** on the panel:
   - Direct external redirect → captured.
   - Greenhouse popup → autofill the form, submit (if
     `SUBMIT_GREENHOUSE=true`), handle email verification if asked,
     log success / manual-required / skip accordingly.
6. Closes the external tab, dismisses the **"Did you apply?"** modal,
   closes the detail panel, and goes again.
7. Stops when there are no more visible jobs after a final scroll.

---

## 🗂️ Output files

All three default paths live under the gitignored `logs/` tree.

| File | Default | What goes in it |
| --- | --- | --- |
| `APPLIED_URLS_FILE` | `logs/jobright/big_log.jsonl` | Every confirmed or manual application, with company / role / timestamp. Used by the dedup window. |
| `SKIPPED_URLS_FILE` | `logs/jobright/skipped_urls.jsonl` | Non-Greenhouse portals, judge SKIPs, recently-applied companies, dismissed submit failures. |
| `MANUAL_REQUIRED_FILE` | `logs/jobright/manual_required.jsonl` | Failed Greenhouse submits during unattended runs (`MANUAL_APPLY_ALERT=false`). |

Each line is JSON, e.g.:

```json
{"applied_at": "2026-04-29T07:52:13Z", "external_url": "https://job-boards.greenhouse.io/...", "company_name": "Acme", "role_title": "Senior Backend Engineer"}
```

---

## ⚙️ Configuration (`.env`)

### Jobright sign-in

| Variable | Default | Purpose |
| --- | --- | --- |
| `JOBRIGHT_EMAIL` | _required_ | Sign-in email. |
| `JOBRIGHT_PASSWORD` | _required_ | Sign-in password. |
| `POST_LOAD_WAIT_SECONDS` | `3` | Wait after the homepage loads before checking for the auto-redirect. |
| `REDIRECT_WAIT_SECONDS` | `30` | Max wait for the post-login redirect to `/jobs/recommend`. |

### Browser

| Variable | Default | Purpose |
| --- | --- | --- |
| `BROWSER_PROFILE_DIR` | `.pw_profile` | Persistent Chromium user-data dir. Delete this to nuke a borked session. |
| `BROWSER_HEADLESS` | `false` | `true` runs Chromium without a window. Recommended for unattended runs once everything works. |
| `ACTION_TIMEOUT_MS` | `15000` | Default per-action timeout. |
| `BROWSER_PROXY` | _empty_ | Optional proxy. Accepts `host:port`, `host:port:user:pass`, `user:pass@host:port`, or full URLs (`http://...`, `socks5://...`). |

### Output / behaviour

| Variable | Default | Purpose |
| --- | --- | --- |
| `APPLIED_URLS_FILE` | `logs/jobright/big_log.jsonl` | See above. |
| `SKIPPED_URLS_FILE` | `logs/jobright/skipped_urls.jsonl` | See above. |
| `MANUAL_REQUIRED_FILE` | `logs/jobright/manual_required.jsonl` | See above. |
| `MANUAL_APPLY_ALERT` | `true` | Pop a Windows MessageBox when a Greenhouse submit doesn't confirm. Set `false` for overnight runs. |
| `COMPANY_DEDUP_DAYS` | `3` | Skip a job if the same company appears in the applied log within this many days. `0` disables. |
| `APPLIED_LOG_RETENTION_DAYS` | `30` | On startup, drop applied-log records older than this. `0` disables pruning. |
| `SUBMIT_GREENHOUSE` | `true` | Actually click **Submit application**. `false` fills the form but leaves it open for review. |

### Backfill

| Variable | Default | Purpose |
| --- | --- | --- |
| `BACKFILL_STOP_AFTER` | `1w` | Stop the backfill at the first card older than this. Format: `<int><unit>` where unit is `s`/`sec`, `min`, `h`/`hr`, `d`, `w`, `m`/`mo` (months — yes, bare `m` means months to match jobright's tags), `y`. |

### Gmail (Greenhouse "verify your email")

| Variable | Default | Purpose |
| --- | --- | --- |
| `GMAIL_ADDRESS` | _empty_ | Inbox to poll for the verification code. |
| `GMAIL_APP_PASSWORD` | _empty_ | A [Gmail app password](https://support.google.com/accounts/answer/185833) — **not** your real password. 🔐 |
| `EMAIL_VERIFY_TIMEOUT_SECONDS` | `90` | Max wait for the code email to arrive. |
| `EMAIL_VERIFY_DELETE_ON_SUCCESS` | `true` | Delete the verification email after a confirmed submit. |

### AI (free-form question answers)

| Variable | Default | Purpose |
| --- | --- | --- |
| `AI_PROVIDER` | _empty_ | Set to `openai` to enable AI answers. Leave blank for safe fallbacks. |
| `OPENAI_API_KEY` | _empty_ | Your OpenAI API key. |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model used for question answering. |
| `AI_PROMPT_FILE` | `ai_prompt.txt` | Editable prompt template. Tweak this to change tone, length, or salary preferences. |

### Applicant profile

The `APPLICANT_*` variables are what the autobid types into the form.
The `.env.example` is heavily commented; the highlights:

- **Identity**: `APPLICANT_FIRST_NAME`, `APPLICANT_LAST_NAME`,
  `APPLICANT_PREFERRED_NAME`, `APPLICANT_EMAIL`, `APPLICANT_PHONE`,
  `APPLICANT_COUNTRY`, `APPLICANT_LINKEDIN`, `APPLICANT_WEBSITE`,
  `APPLICANT_GITHUB`.
- **Address**: `APPLICANT_CITY`, `APPLICANT_STATE`,
  `APPLICANT_STATE_ABBR`, `APPLICANT_POSTAL_CODE`,
  `APPLICANT_LOCATION_CITY`.
- **Education**: `APPLICANT_SCHOOL`, `APPLICANT_DEGREE`,
  `APPLICANT_DISCIPLINE`, plus start/end month + year.
- **Resumes**: `APPLICANT_RESUMES_DIR` — folder of paired `.docx` +
  `.pdf` variants. **Required.** No resumes, no applies. 🚫
- **EEO self-id**: `APPLICANT_GENDER`,
  `APPLICANT_HISPANIC_ETHNICITY`, `APPLICANT_RACE`,
  `APPLICANT_VETERAN_STATUS`, `APPLICANT_DISABILITY_STATUS`. Values
  must match the option labels exactly (or use
  `Decline To Self Identify` and friends).
- **U.S. Standard Demographic survey** (some boards add this on top):
  `APPLICANT_GENDER_IDENTITY`, `APPLICANT_RACIAL_ETHNIC_BACKGROUND`,
  `APPLICANT_SEXUAL_ORIENTATION`, `APPLICANT_TRANSGENDER`,
  `APPLICANT_DISABILITY_CHRONIC`, `APPLICANT_VETERAN_ACTIVE`.
- **Misc**: `APPLICANT_REFERRAL_SOURCE`,
  `APPLICANT_SECURITY_CLEARANCE`, `APPLICANT_SUMMARY` (free-text
  candidate summary the AI prompt uses).

---

## 🧪 Testing the autobid in isolation

You usually don't want to debug the autobid by running the full apply
loop against a live jobright account. Use the test CLI instead — it
walks URLs from `big_log.jsonl` (or anywhere you point it), filters to
Greenhouse, and by default holds the tab open for review **without**
submitting:

```powershell
# Fill the FIRST greenhouse application from the log; hold 30s for review
python -m greezik.test_autobid --limit 1 --hold 30

# Fill an arbitrary URL
python -m greezik.test_autobid --url "https://job-boards.greenhouse.io/embed/job_app?for=...&token=..."

# DANGER ZONE — actually submit one application
python -m greezik.test_autobid --limit 1 --submit
```

Available flags:

- `--source PATH` — JSONL source file (default `logs/jobright/big_log.jsonl`)
- `--url URL` — process this URL instead of the JSONL (repeatable)
- `--limit N` — at most N URLs
- `--submit` — actually submit (default is **dry run**)
- `--hold SECONDS` — keep the tab open this long after fill (dry-run only)
- `--headless` — run without a visible browser window
- `--verbose` — DEBUG-level logs

The test runner uses a **fresh non-persistent Chromium context** so it
won't pollute the main bot's `.pw_profile/`.

---

## 🔧 Troubleshooting

**Sign-in is stuck or weird.** Nuke the persistent profile and try
again:

```powershell
Remove-Item -Recurse -Force .pw_profile
```

**Every job is being skipped.** Almost always a missing or
mis-pointed `APPLICANT_RESUMES_DIR`. Check that the folder exists and
contains paired `.docx` + `.pdf` files with matching stems.

**Greenhouse asked for an email verification code and the bot gave
up.** Make sure `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` are set (and
that `GMAIL_APP_PASSWORD` is an *app password*, not your account
password), and bump `EMAIL_VERIFY_TIMEOUT_SECONDS` if your inbox is
slow.

**Bot pops a dialog at 3am while you're trying to sleep.** That'll be
`MANUAL_APPLY_ALERT=true`. Set it to `false` for unattended runs and
the failed submits will end up in `manual_required.jsonl` for you to
deal with in the morning. ☀️

**OpenAI bills are climbing and you don't want AI answers anymore.**
Leave `AI_PROVIDER` blank. The bot will fall back to safe defaults for
free-form questions.

---

## 🚦 Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Clean stop — no more apply buttons (or backfill hit the stop threshold). |
| `2` | Login failed — could not reach `/jobs/recommend` after sign-in. |
| `3` | Unhandled error — check the logs under `logs/`. |

---

## 🗺️ Project layout

```
greezik/
  main.py             # CLI: `apply` (default) and `backfill` subcommands
  config.py           # .env parsing
  profile.py          # ApplicantProfile loaded from .env
  browser.py          # Playwright launcher (persistent + proxy)
  auth.py             # Jobright sign-in
  applier.py          # The recommend-feed loop
  applied_backfill.py # The /jobs/applied backfill loop
  jobright_jd.py      # JD scraping from the detail panel
  jd_judge.py         # BID/SKIP wrapper around steps/step0_judge_jd.py
  resume_match.py     # Per-job resume scoring (wraps steps/step1_match_resume.py)
  resume_index.py     # Builds the in-memory resume index
  autobid.py          # Greenhouse form filler (the big one)
  ai_answer.py        # OpenAI prompt + answer routing
  email_verify.py     # Gmail IMAP poller for the verification code
  selectors.py        # Centralised CSS / XPath selectors
  storage.py          # Applied / Skipped / ManualRequired JSONL stores
  notifications.py    # Windows MessageBox helpers
  logging_setup.py    # File + console logging config
  test_autobid.py     # Standalone autobid test CLI
```

---

## ⚠️ A friendly note on responsibility

Greezik clicks the same buttons you would — just much faster, more
patiently, and without crying. That said: **you** are the one applying
to those jobs. Read your resumes, sanity-check your `.env`, eyeball
the `ai_prompt.txt` template, and maybe skim the first few applied
URLs the first time you let it loose. The bot is a power tool. Don't
saw your own leg off. 🪚

Happy hunting. 🎣
