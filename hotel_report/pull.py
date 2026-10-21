"""Step 1 · Clean and validate last week's hotel data.

Pure functions only (no network), so everything here is unit-tested.
Input : raw rows from the Google Sheet, as a list of dicts (header -> cell)
Output: one clean dict per hotel, with the reporting period and a status of
        "ok" or "data_check" (plus the reasons why).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# 1. Reporting period = the previous Monday–Sunday
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    @property
    def week_key(self) -> str:  # ISO week, e.g. "2026-W42"
        year, week, _ = self.start.isocalendar()
        return f"{year}-W{week:02d}"

    @property
    def label(self) -> str:  # "Mon 12 – Sun 18 Oct 2026"
        s, e = self.start, self.end
        if s.year != e.year:
            return f"{s:%a} {s.day} {s:%b %Y} – {e:%a} {e.day} {e:%b %Y}"
        if s.month != e.month:
            return f"{s:%a} {s.day} {s:%b} – {e:%a} {e.day} {e:%b %Y}"
        return f"{s:%a} {s.day} – {e:%a} {e.day} {e:%b %Y}"


def reporting_period(now: datetime, tz: str, week_start: str | None = None) -> Period:
    """Last Mon–Sun in the given time zone, or the week containing `week_start`."""
    if week_start:
        try:
            anchor = date.fromisoformat(week_start.strip())
        except ValueError:
            raise ValueError(f'REPORT_WEEK_START "{week_start}" is not a valid date (use YYYY-MM-DD).')
    else:
        anchor = now.astimezone(ZoneInfo(tz)).date() - timedelta(weeks=1)
    start = anchor - timedelta(days=anchor.weekday())  # Monday
    return Period(start, start + timedelta(days=6))


# ---------------------------------------------------------------------------
# 2. Map whatever the headers are called onto standard names
# ---------------------------------------------------------------------------

HEADER_ALIASES = {
    "hotel": ["brandname", "brand name", "brand", "hotel", "hotel name", "property"],
    "revenue": ["revenue", "weekly revenue", "sales"],
    "orders": ["order", "orders", "bookings", "booking count"],
    "owner_email": ["boss gmail", "boss email", "owner email", "owner gmail", "email"],
    "week": ["week", "week start", "week_start", "date", "period start"],  # optional
}
REQUIRED = ["hotel", "revenue", "orders", "owner_email"]


def _norm_header(h: str) -> str:
    return re.sub(r"[_\s]+", " ", str(h).lower()).strip()


_ALIAS_TO_FIELD = {_norm_header(a): f for f, aliases in HEADER_ALIASES.items() for a in aliases}


class SheetFormatError(Exception):
    """The sheet itself is unusable (empty, missing columns). Stops the run."""


# ---------------------------------------------------------------------------
# 3. Helpers for messy cell values
# ---------------------------------------------------------------------------


def _blank(v) -> bool:
    return v is None or str(v).strip() == ""


def to_number(v):
    """'NT$ 92,000' -> 92000.0 · 92000 -> 92000 · '' -> None · 'abc' -> nan"""
    if _blank(v):
        return None
    if isinstance(v, bool):
        return math.nan
    if isinstance(v, (int, float)):
        return float(v)
    cleaned = re.sub(r"[^0-9.\-]", "", str(v))
    try:
        return float(cleaned)
    except ValueError:
        return math.nan


def to_date(v):
    """'2026-10-12' / '12/10/2026' / Sheets serial 46307 -> date. None if blank, False if unreadable."""
    if _blank(v):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return date(1899, 12, 30) + timedelta(days=int(v))
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return False


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_HAN = r"㐀-䶿一-鿿豈-﫿"
_NAME_RE = re.compile(rf"^([{_HAN}][{_HAN}\s]*?)\s+([A-Za-z0-9].*)$")


def split_name(name: str) -> tuple[str, str]:
    """'海角小屋 Cape Cottage' -> ('海角小屋', 'Cape Cottage')"""
    m = _NAME_RE.match(name)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    if re.search(f"[{_HAN}]", name):
        return name, ""
    return "", name


def _fmt_num(x: float) -> int | float:
    return int(x) if x.is_integer() else x


# ---------------------------------------------------------------------------
# 4–6. Clean the rows
# ---------------------------------------------------------------------------


def clean_rows(raw_rows: list[dict], period: Period, pulled_at: datetime) -> list[dict]:
    if not raw_rows:
        raise SheetFormatError("The sheet returned no rows. Check SHEET_ID and SHEET_TAB.")

    # Map headers
    seen_headers: list[str] = []
    rows = []
    for i, raw in enumerate(raw_rows):
        row = {"source_row": raw.get("row_number", i + 2)}  # +2 = header row + 1-based
        for key, value in raw.items():
            if key == "row_number":
                continue
            if key not in seen_headers:
                seen_headers.append(key)
            field = _ALIAS_TO_FIELD.get(_norm_header(key))
            if field and field not in row:
                row[field] = value
        rows.append(row)

    missing = [f for f in REQUIRED if not any(f in r for r in rows)]
    if missing:
        raise SheetFormatError(
            f"Missing column(s): {', '.join(missing)}. Columns found: {', '.join(seen_headers)}."
        )

    # Keep only last week's rows, if there is a date column.
    # The mock sheet has none, so the whole sheet is treated as last week.
    has_week = any(not _blank(r.get("week")) for r in rows)
    period_source = "date column" if has_week else "assumed: sheet has no date column"
    in_period = []
    for r in rows:
        if has_week:
            d = to_date(r.get("week"))
            if d is False:
                r["_bad_date"] = True  # flag it, don't silently drop it
            elif d is None or not (period.start <= d <= period.end):
                continue
        in_period.append(r)

    # Clean each row
    cleaned = []
    for r in in_period:
        if all(_blank(r.get(f)) for f in REQUIRED):
            continue  # fully blank row

        issues: list[str] = []
        if _blank(r.get("hotel")):
            hotel = f"(unnamed hotel, row {r['source_row']})"
            issues.append("hotel name is missing")
        else:
            hotel = re.sub(r"\s+", " ", str(r["hotel"])).strip()
        if r.get("_bad_date"):
            issues.append(f'date "{r["week"]}" could not be read')

        revenue, orders = to_number(r.get("revenue")), to_number(r.get("orders"))

        if revenue is None:
            issues.append("revenue is missing")
        elif math.isnan(revenue):
            issues.append(f'revenue "{r["revenue"]}" is not a number')
        elif revenue < 0:
            issues.append("revenue is negative")

        if orders is None:
            issues.append("orders is missing")
        elif math.isnan(orders):
            issues.append(f'orders "{r["orders"]}" is not a number')
        elif orders < 0:
            issues.append("orders is negative")
        elif not orders.is_integer():
            issues.append("orders is not a whole number")

        usable = all(x is not None and not math.isnan(x) and x >= 0 for x in (revenue, orders))
        if usable:
            if revenue == 0 and orders == 0:
                issues.append("revenue and orders are both 0 (closed, no bookings, or missing data?)")
            elif orders == 0:
                issues.append("revenue recorded but 0 orders")
            elif revenue == 0:
                issues.append("orders recorded but 0 revenue")

        email = "" if _blank(r.get("owner_email")) else str(r["owner_email"]).strip().lower()
        email_valid = bool(EMAIL_RE.match(email))
        # An email problem doesn't make the numbers wrong, so it doesn't change status
        email_issue = (
            "owner email is missing" if not email
            else None if email_valid
            else f'owner email "{email}" is not valid'
        )

        zh, en = split_name(hotel)
        finite = lambda x: x is not None and not math.isnan(x)  # noqa: E731
        cleaned.append({
            "week_key": period.week_key,
            "period_start": period.start.isoformat(),
            "period_end": period.end.isoformat(),
            "period_label": period.label,
            "hotel": hotel,
            "hotel_name_en": en,
            "hotel_name_zh": zh,
            "revenue": _fmt_num(revenue) if finite(revenue) else None,
            "orders": _fmt_num(orders) if finite(orders) else None,
            "owner_email": email,
            "email_valid": email_valid,
            "status": "data_check" if issues else "ok",
            "issues": issues + ([email_issue] if email_issue else []),
            "period_source": period_source,
            "source_row": r["source_row"],
        })

    # Duplicates: identical rows are merged; conflicting rows are both flagged
    groups: dict[str, list[dict]] = {}
    for row in cleaned:
        groups.setdefault(row["hotel"].lower(), []).append(row)

    output = []
    for group in groups.values():
        first = group[0]
        if len(group) == 1:
            output.append(first)
            continue
        rows_list = ", ".join(str(g["source_row"]) for g in group)
        same = all(
            (g["revenue"], g["orders"], g["owner_email"]) == (first["revenue"], first["orders"], first["owner_email"])
            for g in group
        )
        if same:
            first["duplicate_rows_merged"] = rows_list
            output.append(first)
        else:
            for g in group:
                g["status"] = "data_check"
                g["issues"].append(f"conflicting duplicate rows for this hotel (rows {rows_list})")
                output.append(g)

    if not output:
        raise SheetFormatError(f"No hotel rows for {period.label}.")

    for row in output:
        row["issues_text"] = "; ".join(row["issues"])
        row["history_key"] = f"{row['hotel']}|{row['week_key']}"  # one History row per hotel per week
        row["pulled_at"] = pulled_at.isoformat(timespec="seconds")
    return sorted(output, key=lambda r: r["source_row"])
