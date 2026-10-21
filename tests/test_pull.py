"""Tests for Step 1. Run: python -m pytest -q"""
from datetime import datetime
from pathlib import Path

import pytest

from hotel_report.pull import SheetFormatError, clean_rows, reporting_period
from hotel_report.sheets import HISTORY_COLUMNS, plan_history_upsert, rows_from_csv

TZ = "Asia/Kuala_Lumpur"
MOCK = rows_from_csv((Path(__file__).parent / "mock_sheet.csv").read_text(encoding="utf-8"))
NOW = datetime.fromisoformat("2026-10-21T10:16:00+08:00")


def run(rows, now=NOW, week_start=None):
    period = reporting_period(now, TZ, week_start)
    return clean_rows(rows, period, pulled_at=now)


def find(out, name):
    return [r for r in out if r["hotel"] == name or r["hotel_name_en"] == name]


# --- Mock data from the assignment ----------------------------------------

out = run(MOCK)


def test_returns_all_10_hotels():
    assert len(out) == 10


def test_reporting_period_is_last_mon_to_sun():
    r = out[0]
    assert (r["period_label"], r["period_start"], r["period_end"], r["week_key"]) == (
        "Mon 12 – Sun 18 Oct 2026", "2026-10-12", "2026-10-18", "2026-W42")


def test_cape_cottage_flagged_for_data_check():
    cape = find(out, "Cape Cottage")[0]
    assert cape["status"] == "data_check"
    assert "both 0" in cape["issues_text"]


def test_other_nine_are_ok():
    assert sum(r["status"] == "ok" for r in out) == 9


def test_csv_text_becomes_numbers():
    sky = find(out, "Sky Spa Resort")[0]
    assert (sky["revenue"], sky["orders"]) == (598000, 102)
    assert isinstance(sky["orders"], int)


def test_names_split_into_chinese_and_english():
    bb = find(out, "Blue Bay Hostel")[0]
    assert (bb["hotel_name_zh"], bb["hotel_name_en"]) == ("藍灣青年旅舍", "Blue Bay Hostel")


def test_history_key_is_hotel_and_week():
    assert out[1]["history_key"] == "森丘山居 Forest Hill|2026-W42"


def test_no_date_column_means_period_is_assumed():
    assert "assumed" in out[0]["period_source"]


# --- Dates and time zones --------------------------------------------------

@pytest.mark.parametrize("now, label, week", [
    ("2026-10-26T09:00:00+08:00", "Mon 19 – Sun 25 Oct 2026", "2026-W43"),       # Monday run
    ("2026-10-07T09:00:00+08:00", "Mon 28 Sep – Sun 4 Oct 2026", "2026-W40"),    # across months
    ("2027-01-04T09:00:00+08:00", "Mon 28 Dec 2026 – Sun 3 Jan 2027", "2026-W53"),  # across years
    ("2026-10-25T17:00:00+00:00", "Mon 19 – Sun 25 Oct 2026", "2026-W43"),       # 01:00 Mon MYT = Sun UTC
])
def test_period(now, label, week):
    p = reporting_period(datetime.fromisoformat(now), TZ)
    assert (p.label, p.week_key) == (label, week)


def test_github_cron_time_gives_the_right_week():
    # The Action runs at 01:00 UTC Monday; that's 09:00 Monday in Malaysia
    p = reporting_period(datetime.fromisoformat("2026-10-26T01:00:00+00:00"), TZ)
    assert p.label == "Mon 19 – Sun 25 Oct 2026"


def test_week_override_snaps_to_monday():
    assert reporting_period(NOW, TZ, "2026-10-14").label == "Mon 12 – Sun 18 Oct 2026"


def test_bad_override_raises_clear_error():
    with pytest.raises(ValueError, match="not a valid date"):
        reporting_period(NOW, TZ, "12 Oct")


# --- Messy sheet data ------------------------------------------------------

def row(n, name, orders, revenue, email):
    return {"row_number": n, "Hotel Name": name, "Orders": orders, "Revenue": revenue, "Owner Email": email}


MESSY = [
    row(2, "Alpha Inn", " 21 ", "NT$ 92,000", " Owner@Alpha.COM "),
    row(3, "", "", "", ""),
    row(4, "Beta Hotel", 10, "abc", "b@beta.com"),
    row(5, "Gamma Lodge", 5, -100, "g@gamma.com"),
    row(6, "Delta Stay", 2.5, 1000, "d@delta.com"),
    row(7, "Echo Rooms", 9, 45000, ""),
    row(8, "Foxtrot  Suites", 9, 45000, "not-an-email"),
    row(9, "Golf House", 30, 150000, "g@golf.com"),
    row(10, "Golf House", 30, 150000, "g@golf.com"),
    row(11, "Hotel Ten", 30, 150000, "h@ten.com"),
    row(12, "hotel ten", 31, 151000, "h@ten.com"),
    row(13, "India Inn", 0, 5000, "i@india.com"),
    row(14, "", 4, 8000, "x@x.com"),
]
m = run(MESSY)


def test_renamed_headers_and_values_cleaned():
    a = find(m, "Alpha Inn")[0]
    assert (a["revenue"], a["orders"], a["owner_email"], a["status"]) == (92000, 21, "owner@alpha.com", "ok")


def test_blank_row_skipped():
    assert not any(r["source_row"] == 3 for r in m)


@pytest.mark.parametrize("name, text", [
    ("Beta Hotel", "not a number"),
    ("Gamma Lodge", "negative"),
    ("Delta Stay", "whole number"),
    ("India Inn", "0 orders"),
])
def test_bad_numbers_go_to_data_check(name, text):
    r = find(m, name)[0]
    assert r["status"] == "data_check" and text in r["issues_text"]


def test_missing_email_keeps_numbers_ok():
    e = find(m, "Echo Rooms")[0]
    assert e["status"] == "ok" and e["email_valid"] is False and "email is missing" in e["issues_text"]


def test_invalid_email_and_double_space_name():
    f = find(m, "Foxtrot Suites")[0]
    assert f["email_valid"] is False


def test_identical_duplicates_merged():
    g = find(m, "Golf House")
    assert len(g) == 1 and g[0]["status"] == "ok" and g[0]["duplicate_rows_merged"] == "9, 10"


def test_conflicting_duplicates_both_flagged():
    t = [r for r in m if r["hotel"].lower() == "hotel ten"]
    assert len(t) == 2 and all(r["status"] == "data_check" and "conflicting" in r["issues_text"] for r in t)


def test_missing_hotel_name_flagged_with_row():
    u = [r for r in m if r["source_row"] == 14][0]
    assert u["hotel"] == "(unnamed hotel, row 14)" and u["status"] == "data_check"


# --- Sheets with a date column --------------------------------------------

DATED = [
    {"row_number": 2, "brandname": "Alpha Inn", "revenue": 1000, "Order": 2, "Boss Gmail": "a@a.com", "week_start": "2026-10-05"},
    {"row_number": 3, "brandname": "Alpha Inn", "revenue": 2000, "Order": 4, "Boss Gmail": "a@a.com", "week_start": "2026-10-12"},
    {"row_number": 4, "brandname": "Beta Hotel", "revenue": 3000, "Order": 6, "Boss Gmail": "b@b.com", "week_start": 46307},
    {"row_number": 5, "brandname": "Gamma Lodge", "revenue": 3000, "Order": 6, "Boss Gmail": "g@g.com", "week_start": "last week"},
]
d = run(DATED)


def test_keeps_only_last_weeks_rows():
    alpha = find(d, "Alpha Inn")
    assert len(alpha) == 1 and alpha[0]["revenue"] == 2000 and d[0]["period_source"] == "date column"


def test_reads_sheets_serial_dates():
    assert find(d, "Beta Hotel")


def test_unreadable_date_flagged_not_dropped():
    g = find(d, "Gamma Lodge")[0]
    assert g["status"] == "data_check" and "could not be read" in g["issues_text"]


# --- Failures that should stop the run loudly ------------------------------

def test_empty_sheet_raises():
    with pytest.raises(SheetFormatError, match="no rows"):
        run([])


def test_missing_column_raises_and_lists_found_columns():
    with pytest.raises(SheetFormatError, match=r"Missing column\(s\): orders\. Columns found: brandname, revenue, Boss Gmail"):
        run([{"row_number": 2, "brandname": "X", "revenue": 1, "Boss Gmail": "x@x.com"}])


# --- History upsert ---------------------------------------------------------

def test_history_first_run_writes_header_and_appends_all():
    header_needed, updates, appends = plan_history_upsert([], out)
    assert header_needed and not updates and len(appends) == 10
    assert appends[0][HISTORY_COLUMNS.index("issues")] == out[0]["issues_text"]


def test_history_rerun_updates_instead_of_duplicating():
    existing = [HISTORY_COLUMNS] + [[r["history_key"]] + [""] * 11 for r in out[:4]]
    _, updates, appends = plan_history_upsert(existing, out)
    assert [n for n, _ in updates] == [2, 3, 4, 5] and len(appends) == 6


def test_history_wrong_header_raises():
    with pytest.raises(ValueError, match="first row"):
        plan_history_upsert([["hotel", "revenue"]], out)
