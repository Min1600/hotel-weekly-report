"""Weekly hotel report: entry point (GitHub Actions runs this every Monday).

  Step 1  Pull     read the sheet, clean it, work out "last week", save History
  Step 2  Classify compare revenue / orders / avg order value with the median
  Step 3  Write    Gemini writes a short summary; code checks every number
  Step 4  Send     email each owner (your inbox in test mode), log it

Settings come from environment variables (GitHub secrets/variables, or .env):
  SHEET_ID                     ID of YOUR COPY of the Google Sheet
  SHEET_TAB / HISTORY_TAB / RUN_LOG_TAB   default Sheet1 / History / Run Log
  TIMEZONE                     default Asia/Kuala_Lumpur
  REPORT_WEEK_START            optional YYYY-MM-DD to re-run a past week
  GOOGLE_SERVICE_ACCOUNT_JSON  read + write the sheet (else: public CSV, no logs)
  CSV_FILE                     local CSV instead of the sheet (offline testing)
  GEMINI_API_KEY / GEMINI_MODEL           AI; without a key the template is used
  GMAIL_ADDRESS / GMAIL_APP_PASSWORD      sending; without them, previews only
  TEST_MODE                    "true" (default) sends everything to TEST_RECIPIENT
  TEST_RECIPIENT / INTERNAL_EMAIL         your inbox / who gets data-check alerts
  DRY_RUN                      "true" = build previews, send nothing
  FORCE_RESEND                 "true" = ignore the Run Log and send again
  CURRENCY / SENDER_NAME       default NT$ / Partner Success Team
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from hotel_report import sheets
from hotel_report.pipeline import run_pipeline
from hotel_report.pull import SheetFormatError, clean_rows, reporting_period
from hotel_report.send import RUN_LOG_COLUMNS, GmailSender, SendSettings
from hotel_report.write import gemini_generator


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def flag(name: str, default: bool) -> bool:
    v = env(name)
    return default if v == "" else v.lower() in ("1", "true", "yes", "on")


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    load_dotenv()
    tz = env("TIMEZONE", "Asia/Kuala_Lumpur")
    now = datetime.now(timezone.utc)
    period = reporting_period(now, tz, env("REPORT_WEEK_START") or None)
    print(f"Reporting period: {period.label} ({period.week_key})")

    settings = SendSettings(
        test_mode=flag("TEST_MODE", True),            # safe by default
        test_recipient=env("TEST_RECIPIENT"),
        internal_email=env("INTERNAL_EMAIL") or env("TEST_RECIPIENT"),
        sender_name=env("SENDER_NAME", "Partner Success Team"),
        gmail_address=env("GMAIL_ADDRESS"),
        gmail_app_password=env("GMAIL_APP_PASSWORD").replace(" ", ""),
        dry_run=flag("DRY_RUN", False),
    )
    if settings.test_mode and not settings.test_recipient and settings.can_send:
        print("ERROR: TEST_MODE is on, so set TEST_RECIPIENT (your own email).", file=sys.stderr)
        return 1
    print(f"Mode: {settings.mode}{' (dry run)' if not settings.can_send else ''}")

    # ---- Step 1: pull -----------------------------------------------------
    sheet_id, creds, csv_file = env("SHEET_ID"), env("GOOGLE_SERVICE_ACCOUNT_JSON"), env("CSV_FILE")
    spreadsheet = None
    if csv_file:
        raw = sheets.rows_from_csv(Path(csv_file).read_text(encoding="utf-8-sig"))
        source = f"local file {csv_file}"
    elif not sheet_id:
        print("ERROR: set SHEET_ID (the long ID in your sheet copy's URL).", file=sys.stderr)
        return 1
    elif creds:
        spreadsheet = sheets.open_spreadsheet(sheet_id, creds)
        raw = sheets.read_with_service_account(spreadsheet, env("SHEET_TAB", "Sheet1"))
        source = "Google Sheets (service account)"
    else:
        raw = sheets.read_public_csv(sheet_id, env("SHEET_TAB", "Sheet1"))
        source = "Google Sheets (public CSV link)"
    print(f"Read {len(raw)} rows from {source}")

    try:
        rows = clean_rows(raw, period, pulled_at=now.astimezone())
    except SheetFormatError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    out = Path("output")
    out.mkdir(exist_ok=True)
    (out / f"step1_clean_{period.week_key}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if spreadsheet is not None:
        print(sheets.save_history(spreadsheet, env("HISTORY_TAB", "History"), rows))

    # ---- Steps 2–4 --------------------------------------------------------
    api_key = env("GEMINI_API_KEY")
    generate = gemini_generator(api_key, env("GEMINI_MODEL", "gemini-3.5-flash-lite")) if api_key else None
    if not api_key:
        print("No GEMINI_API_KEY: summaries will use the fixed template.")

    log_tab = env("RUN_LOG_TAB", "Run Log")
    already_sent = sheets.read_sent_keys(spreadsheet, log_tab, RUN_LOG_COLUMNS) if spreadsheet else set()

    def go(sender):
        return run_pipeline(
            rows, currency=env("CURRENCY", "NT$"), settings=settings, generate=generate,
            sender=sender, already_sent=already_sent, force_resend=flag("FORCE_RESEND", False),
            out_dir=out / "emails", now=now.astimezone(),
        )

    if settings.can_send:
        with GmailSender(settings) as sender:
            result = go(sender)
    else:
        result = go(None)

    if spreadsheet is not None:
        sheets.append_run_log(spreadsheet, log_tab, RUN_LOG_COLUMNS, result.log_entries)

    # ---- Report -----------------------------------------------------------
    bm = result.benchmarks
    print(f"Benchmarks (median of {bm.hotels_used} hotels): revenue {bm.revenue:,.0f} · "
          f"orders {bm.orders:,.0f} · avg order value {bm.aov:,.0f}")
    for o in result.outcomes:
        print(f"  {o.hotel:32s} {o.overall:18s} {o.status:34s} {o.ai_status}  {o.error}")
    print(f"Email previews saved in {out / 'emails'}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = [
            f"### Weekly hotel report · {period.label} · {settings.mode} mode",
            "",
            f"Benchmarks (median of {bm.hotels_used} hotels): revenue {bm.revenue:,.0f} · "
            f"orders {bm.orders:,.0f} · avg order value {bm.aov:,.0f}",
            "",
            "| Hotel | Overall | Email | Sent to | Writer | Notes |",
            "|---|---|---|---|---|---|",
            *[f"| {o.hotel} | {o.overall} | {o.status} | {o.sent_to} | {o.ai_status} | {o.error} |" for o in result.outcomes],
        ]
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
