# PubMed Biological Variation alert

Weekly automated PubMed alert. Runs every Monday at 07:00 UTC on GitHub Actions,
queries PubMed for new articles on biological variation, and emails an HTML
digest to the configured recipients.

## Repository layout

```
.
├── pubmed_alert.py             # main script
├── requirements.txt            # Python deps (requests)
├── seen_pmids.json             # state: PMIDs already emailed (auto-updated)
└── .github/
    └── workflows/
        └── weekly_alert.yml    # GitHub Actions schedule
```

> The workflow file **must** live at `.github/workflows/weekly_alert.yml`.
> When you upload the file to GitHub, create those folders.

## One-time setup

### 1. Create a Gmail App Password

Needed because Gmail no longer allows SMTP login with your regular password.

1. The Gmail account used to **send** must have 2-Step Verification enabled:
   <https://myaccount.google.com/security> → "2-Step Verification".
2. Go to <https://myaccount.google.com/apppasswords>.
3. Create an App Password named e.g. `pubmed-alert`. You'll get a 16-character
   code like `abcd efgh ijkl mnop`. Copy it (spaces optional, both work).

You can use `jdgmarco@gmail.com` as the sender (it will email itself plus
`isabelmorenoparro@gmail.com`), or create a dedicated alert account.

### 2. Create the GitHub repo

1. Sign up at <https://github.com> (free).
2. Click **New repository**, name it e.g. `bv-pubmed-alert`. **Private** is
   fine; public also fine — no secrets are in the code.
3. Upload the 4 files in this folder, keeping `weekly_alert.yml` inside
   `.github/workflows/`.

### 3. Add the secrets

In the repo: **Settings → Secrets and variables → Actions → New repository
secret**. Create two secrets:

| Name                  | Value                                            |
| --------------------- | ------------------------------------------------ |
| `GMAIL_USER`          | The sending Gmail address (e.g. `jdgmarco@gmail.com`) |
| `GMAIL_APP_PASSWORD`  | The 16-char App Password from step 1             |

### 4. First run (manual test)

Don't wait for Monday — trigger a manual run:

1. Go to the **Actions** tab in the repo.
2. Select **Weekly PubMed alert** in the sidebar.
3. Click **Run workflow** → **Run workflow** (green button).

Within ~1 minute you should receive the email at both recipient addresses.
If something fails, click the failed run to see the log.

## Tweaks you may want later

All in `pubmed_alert.py` near the top:

- **Recipients** → `RECIPIENTS` list.
- **Search query** → `QUERY` string.
- **Time window** → `RELDATE_DAYS` (default 10, gives a 3-day overlap to
  catch late-indexed papers; dedup handles the overlap).
- **Schedule** → `cron` field in `.github/workflows/weekly_alert.yml`.
  Example for daily 06:30 UTC: `30 6 * * *`.

## Cost

Free. GitHub Actions allows 2,000 minutes/month for private repos and is
unlimited for public ones. This workflow uses ~30 seconds per run.

## Notes

- Uses NCBI E-utilities (no API key required; the script identifies itself
  with `tool` and `email` params per NCBI policy).
- If you want to raise the rate limit (>3 req/s), get a free NCBI API key
  at <https://www.ncbi.nlm.nih.gov/account/settings/> and add it as a
  third secret `NCBI_API_KEY` (small edit needed in the script — ask).
- `seen_pmids.json` is auto-committed by the workflow so duplicates are
  filtered across runs. It caps at 5,000 PMIDs to stay tiny.
