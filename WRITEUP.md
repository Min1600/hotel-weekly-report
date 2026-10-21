# Weekly Hotel Report: Write-up

## What it does
Every Monday at 09:00, a GitHub Action reads last week's figures from the Google Sheet. It
compares each hotel with the other partner hotels, has an AI write a short summary, and
emails it to the owner. While testing, every email goes to my own inbox instead.

## Why these tools

| Need | Tool | Why |
|---|---|---|
| Run every Monday, no server | **GitHub Actions** (cron) | Free and reliable. Each run has a log and a summary table, and I get an email if it fails. The code, schedule and history all live in one repo. |
| Logic | **Python** | Easy to read and test. The cleaning, benchmarks and checks are plain functions with 76 automated tests, and the tests run before every report. |
| Read/write the sheet | **Google Sheets API** (`gspread`, service account) | The data already lives in Sheets. A service account lets the Action log in without a person, and lets it write back the History and Run Log tabs. |
| Write the summaries | **Gemini** | Free API tier and good at short, structured writing. It can return JSON that matches a fixed schema. |
| Structure | **Pydantic** | Defines exactly what goes into the AI (`HotelFacts`) and what must come back (`EmailText`). A malformed reply is rejected automatically. |
| Send | **Gmail SMTP** (App Password) | Uses my own Gmail as the brief suggests. No extra service needed. |

## How I decided "doing well" vs "needs improvement"
The sheet has one week of revenue and orders per hotel. It has no targets and no previous
weeks. So the only fair benchmark available is the other hotels that week.

- **Benchmark = the median** of hotels with valid data. I used the median rather than the
  mean because one large hotel (Sky Spa Resort, NT$598k) would pull the mean up.
  Cape Cottage's zeros are excluded so they don't drag the benchmark down.
- **Three metrics:** revenue, orders, and average order value (revenue ÷ orders).
  For each one, **at or above the median = Doing well; below = Needs improvement.**
  Counting "at" as doing well settles ties: with 9 hotels, one hotel always sits exactly on the median.
- **Overall verdict = the revenue verdict.** Revenue is the outcome owners care about,
  and orders and average order value explain *why* revenue is high or low.
- **Data checks come first.** 0 revenue and 0 orders, missing or negative values, and
  conflicting duplicate rows are never compared. A hotel with 0 revenue and 0 orders gets
  an email asking whether it was closed, had no stays, or the data is wrong. Other problems
  go to an internal alert.

**Results this week** (benchmarks: NT$315,000 revenue · 71 orders · NT$4,675 per order):
Doing well: Sky Spa Resort, Songhe Inn, Ocean View, Starlight Biz, Riverside Hotel.
Needs improvement: Orange Light, Artis Inn, Blue Bay Hostel, Forest Hill.
Data check: Cape Cottage.

**Limitations.** A median split means about half the hotels are "below" every week. It
also compares a hostel with a spa resort. The History tab saves every week, so the next
step would be to compare each hotel with its own last 4 weeks, or with targets if the
business sets them.

## How the AI writes the messages
**Principle: code owns the numbers, the AI owns the wording.**

1. **Facts in.** Code calculates everything and gives Gemini only this hotel's
   `HotelFacts`: name, week, overall verdict, and the three metrics with value, benchmark,
   % difference and verdict. No other hotels and no email addresses, so nothing can leak.
2. **Fixed shape out.** Gemini must reply as `EmailText`: a subject under 90 characters
   and a 2–3 sentence summary. Pydantic rejects anything else.
3. **Instructions.** The AI writes like a warm, factual account manager. It opens with the
   overall result, then covers each metric against the benchmark. It uses only the given
   numbers, exactly as written. It gives no advice, and makes no comparisons with past weeks,
   because there's no data for them.
4. **Checks after the AI.**
   - Every number in the text must appear in the facts.
   - No advice words ("should", "recommend").
   - No trend claims ("doubled", "last year").
   - No other hotel's name, and its own name must appear.
5. **Retry, then fall back.** If a check fails, the AI gets one retry with the reason. If it
   fails again, or the API is down, a fixed template is used. The email still goes out, and
   the Run Log records whether the AI or the template wrote it.
6. **Code builds the email.** The figures table, colours and verdicts come from code. The
   AI's text only fills the summary paragraph, so the table can't be wrong.

## Safety
- **Test mode is on by default.** Every email is redirected to my inbox, and the subject
  shows who it was meant for. One "owner" address in the mock sheet is a real Gmail account,
  so this mattered.
- **No double sends.** The Run Log stores a hotel + week + mode key, so re-running on
  Monday doesn't email anyone twice.
- **Tests run first.** Broken code stops the run before any data is read or any email is sent.

## Assumptions
- The sheet has no dates, so I assumed it holds last week. If a date column is added,
  only rows from last Monday to Sunday are used.
- Currency is assumed to be **NT$**. The hotel names are Traditional Chinese, and ~NT$4,700
  per booking is a normal room rate. It's one setting to change.

## Next Steps

I considered clustering, but with 9 hotels and two independent variables it would be unstable and no more informative than a simple median comparison. With more hotels and property attributes, I’d use clustering to create peer groups for fairer benchmarks. Once the platform has hundreds of hotels and richer data, such as room count, star rating, city, property type, occupancy, cancellations and several weeks of history using unsupervised machine learning to create distinct groups would prove useful and informative.
