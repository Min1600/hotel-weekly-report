"""Google Sheets input/output.

Two ways to read the sheet:
  * Service account (recommended): set GOOGLE_SERVICE_ACCOUNT_JSON and share
    your copy of the sheet with the service account's email. Needed to write History.
  * Public CSV export: if the sheet is shared "Anyone with the link can view",
    reading works with no credentials at all (History is then skipped).
"""
from __future__ import annotations

import csv
import io
import json
import urllib.parse
import urllib.request

HISTORY_COLUMNS = [
    "history_key", "week_key", "period_start", "period_end", "hotel",
    "revenue", "orders", "owner_email", "status", "issues", "period_source", "pulled_at",
]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def rows_from_csv(text: str) -> list[dict]:
    """CSV text -> list of dicts with a row_number, like the Sheets API gives."""
    reader = csv.DictReader(io.StringIO(text))
    return [{"row_number": i, **row} for i, row in enumerate(reader, start=2)]


def read_public_csv(sheet_id: str, tab: str) -> list[dict]:
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq"
        f"?tqx=out:csv&sheet={urllib.parse.quote(tab)}"
    )
    with urllib.request.urlopen(url, timeout=30) as resp:
        text = resp.read().decode("utf-8-sig")
    if text.lstrip().startswith("<"):
        raise PermissionError(
            "Google returned a web page instead of CSV. Share the sheet as "
            "'Anyone with the link can view', or set GOOGLE_SERVICE_ACCOUNT_JSON."
        )
    return rows_from_csv(text)


def open_spreadsheet(sheet_id: str, service_account_json: str):
    import gspread  # imported here so the CSV path needs no extra packages

    client = gspread.service_account_from_dict(json.loads(service_account_json))
    return client.open_by_key(sheet_id)


def read_with_service_account(spreadsheet, tab: str) -> list[dict]:
    ws = spreadsheet.worksheet(tab)
    values = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
    if not values:
        return []
    header, *body = values
    return [
        {"row_number": i, **{h: (r[j] if j < len(r) else "") for j, h in enumerate(header)}}
        for i, r in enumerate(body, start=2)
    ]


# ---------------------------------------------------------------------------
# History: one row per hotel per week, updated in place on re-runs
# ---------------------------------------------------------------------------

def history_row(clean: dict) -> list:
    return [clean["issues_text"] if c == "issues" else clean.get(c, "") for c in HISTORY_COLUMNS]


def plan_history_upsert(existing: list[list], clean_rows: list[dict]):
    """Work out which History rows to overwrite and which to append.

    existing: all values currently in the History tab (first row = headers)
    returns : (header_needed, updates [(sheet_row_number, values)], appends [values])
    """
    header_needed = not existing or not any(existing[0])
    key_col = 0
    if not header_needed:
        if existing[0][: len(HISTORY_COLUMNS)] != HISTORY_COLUMNS:
            raise ValueError(
                "The History tab's first row doesn't match the expected columns: "
                + ", ".join(HISTORY_COLUMNS)
            )
    row_of_key = {}
    for i, r in enumerate(existing[1:] if not header_needed else [], start=2):
        if r and r[key_col]:
            row_of_key[r[key_col]] = i

    updates, appends = [], []
    for c in clean_rows:
        values = history_row(c)
        if c["history_key"] in row_of_key:
            updates.append((row_of_key[c["history_key"]], values))
        else:
            appends.append(values)
    return header_needed, updates, appends


def _get_or_create(spreadsheet, tab: str, columns: list[str]):
    import gspread

    try:
        return spreadsheet.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=tab, rows=1000, cols=len(columns))
        ws.update(values=[columns], range_name="A1")
        return ws


def sent_keys_from_log(existing: list[list]) -> set[str]:
    """log_keys already marked as sent (first column = log_key, 'status' column = sent)."""
    if not existing:
        return set()
    header = existing[0]
    if "log_key" not in header or "status" not in header:
        return set()
    k, s = header.index("log_key"), header.index("status")
    return {r[k] for r in existing[1:] if len(r) > max(k, s) and r[s] == "sent"}


def read_sent_keys(spreadsheet, tab: str, columns: list[str]) -> set[str]:
    return sent_keys_from_log(_get_or_create(spreadsheet, tab, columns).get_all_values())


def append_run_log(spreadsheet, tab: str, columns: list[str], entries: list[dict]) -> None:
    if entries:
        ws = _get_or_create(spreadsheet, tab, columns)
        ws.append_rows([[e.get(c, "") for c in columns] for e in entries], value_input_option="RAW")


def save_history(spreadsheet, tab: str, clean_rows: list[dict]) -> str:
    import gspread

    try:
        ws = spreadsheet.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=tab, rows=1000, cols=len(HISTORY_COLUMNS))

    header_needed, updates, appends = plan_history_upsert(ws.get_all_values(), clean_rows)
    last_col = chr(ord("A") + len(HISTORY_COLUMNS) - 1)
    if header_needed:
        ws.update(values=[HISTORY_COLUMNS], range_name=f"A1:{last_col}1")
    if updates:
        ws.batch_update(
            [{"range": f"A{n}:{last_col}{n}", "values": [v]} for n, v in updates],
            value_input_option="RAW",
        )
    if appends:
        ws.append_rows(appends, value_input_option="RAW")
    return f"History: {len(appends)} added, {len(updates)} updated"
