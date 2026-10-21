# Weekly Hotel Performance Report

Every Monday at 09:00 (Malaysia/Taiwan time), GitHub Actions:

1. **Pulls** last week's data from Google Sheets, cleans it, and saves a History snapshot.
2. **Classifies** each hotel: revenue, orders and average order value are compared with the median of all hotels that week.
3. **Writes** a short summary for each hotel with Gemini. Code checks every number the AI uses.
4. **Sends** each owner an email (your own inbox in test mode) and records it in a Run Log.

Hotels with 0 revenue and 0 orders get a "please confirm your figures" email instead. Any other data problem goes to an internal alert, never to the owner.

See [WRITEUP.md](WRITEUP.md) for why it's built this way.

## Project layout

```
run.py                        entry point (what GitHub Actions runs)
hotel_report/pull.py          Step 1 · clean the sheet, work out "last week"
hotel_report/classify.py      Step 2 · compare with the median benchmark
hotel_report/models.py        Pydantic models: HotelFacts (into the AI), EmailText (out of it)
hotel_report/write.py         Step 3 · Gemini prompt, checks, retry, template fallback
hotel_report/send.py          Step 4 · email layout, test-mode routing, Gmail sending
hotel_report/pipeline.py      Steps 2–4 joined together
hotel_report/sheets.py        Google Sheets read/write (data, History, Run Log)
tests/                        76 pytest tests, with a fake AI and a fake mail server
.github/workflows/weekly-report.yml
```

## Setup (about 20 minutes)

### 1. Your own copy of the sheet
Open the assignment sheet → **File → Make a copy**. The `SHEET_ID` is the long part of
your copy's URL: `docs.google.com/spreadsheets/d/<SHEET_ID>/edit`.

### 2. Google service account (lets the Action read and write your sheet)
1. [Google Cloud Console](https://console.cloud.google.com/) → create a project.
2. **APIs & Services → Library** → enable **Google Sheets API**.
3. **IAM & Admin → Service Accounts** → create one → **Keys → Add key → JSON**.
4. In your sheet copy, click **Share**, add the service account's email (`…@…iam.gserviceaccount.com`) and give it **Editor** access.

### 3. Gemini API key
[Google AI Studio](https://aistudio.google.com/) → **Get API key**. Without a key, every
email uses the fixed template, so the system still works.

### 4. Gmail App Password (for sending)
Your Google account needs 2-Step Verification turned on. Then go to
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) and create one.
It's a 16-character password just for this app. Your normal password won't work here.

### 5. GitHub settings
Push this folder to a new repo → **Settings → Secrets and variables → Actions**.

| Secrets | |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the whole contents of the JSON key file |
| `GEMINI_API_KEY` | from AI Studio |
| `GMAIL_ADDRESS` | the Gmail you send from |
| `GMAIL_APP_PASSWORD` | the 16-character app password |

| Variables | |
|---|---|
| `SHEET_ID` | your copy's ID |
| `TEST_RECIPIENT` | your own email. **All** emails go here while testing |
| `TEST_MODE` | leave unset (= `true`). Set to `false` only to email real owners |
| optional | `INTERNAL_EMAIL`, `CURRENCY` (NT$), `SENDER_NAME`, `GEMINI_MODEL` (gemini-3.5-flash-lite), `TIMEZONE` |

### 6. Test it
**Actions → Weekly hotel report → Run workflow** (tick *dry run* first if you like).

- Your inbox gets 9 summaries, 1 "please confirm" email (Cape Cottage) and 1 internal alert.
  Each subject starts with `[TEST → owner's address]`.
- The run page shows a table of every hotel: verdict, email status, and whether the AI or the template wrote it.
- Your sheet gets **History** and **Run Log** tabs.
- Run it again: nothing is re-sent. To send again anyway, tick *force resend*.
- The **report-output** artifact has every email as `.html` and `.eml`. Good for screenshots.

## Run locally

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q                          # 76 tests
CSV_FILE=tests/mock_sheet.csv python run.py  # offline: no sheet, no AI, no sending
cp .env.example .env                         # fill it in to use the real services
python run.py
```

## Safety switches
- **TEST_MODE** is on unless it's explicitly `false`. Owners can't be emailed by accident.
  (One owner address in the mock sheet is a real Gmail account.)
- **Gmail not set up, or DRY_RUN** → emails are only saved as previews.
- **Run Log** → each hotel is emailed at most once per week per mode. Test sends never block a later live send.
- **Tests run first** in every Action. If they fail, nothing is read or sent.
- If any email fails, the run is marked failed, and GitHub emails you about failed scheduled runs.

## Next Steps

I considered clustering, but with 9 hotels and two independent variables it would be unstable and no more informative than a simple median comparison. With more hotels and property attributes, I’d use clustering to create peer groups for fairer benchmarks. Once the platform has hundreds of hotels and richer data, such as room count, star rating, city, property type, occupancy, cancellations and several weeks of history using unsupervised machine learning to create distinct groups would prove useful and informative.
