"""Runs Steps 2–4 on Step 1's clean rows. Network pieces are passed in, so
the whole pipeline can be tested with a fake AI and a fake mail server."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .classify import Benchmarks, classify
from .models import EmailText, HotelFacts
from .send import (
    SendSettings, build_message, data_check_alert, is_zero_activity, log_key, render_html,
    render_text, route, zero_activity_email,
)
from .write import write_summary


@dataclass
class Outcome:
    hotel: str
    overall: str
    ai_status: str = ""
    status: str = ""             # sent / preview / skipped / held back / failed
    sent_to: str = ""
    subject: str = ""
    error: str = ""


@dataclass
class RunResult:
    benchmarks: Benchmarks
    outcomes: list[Outcome] = field(default_factory=list)
    log_entries: list[dict] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(o.status == "failed" for o in self.outcomes)


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower() or "hotel"


def run_pipeline(
    rows: list[dict],
    *,
    currency: str,
    settings: SendSettings,
    generate: Callable[[str], str] | None,
    sender=None,                       # object with .send(msg); None = previews only
    already_sent: set[str] = frozenset(),
    force_resend: bool = False,
    out_dir: Path = Path("output/emails"),
    now: datetime,
) -> RunResult:
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- Step 2: classify ------------------------------------------------
    facts_list, data_checks, bm = classify(rows, currency)
    result = RunResult(benchmarks=bm)
    all_names = [f.hotel_name_en or f.hotel for f in facts_list] + [r["hotel_name_en"] or r["hotel"] for r in data_checks]

    def deliver(intended: str, subject: str, text_body: str, html_body: str | None, slug: str) -> tuple[str, str, str]:
        to, subj = route(intended, subject, settings)
        msg = build_message(to, subj, text_body, html_body, settings)
        (out_dir / f"{slug}.eml").write_bytes(bytes(msg))
        if html_body:
            (out_dir / f"{slug}.html").write_text(html_body, encoding="utf-8")
        if sender is None:
            return "preview", to, subj
        sender.send(msg)
        return "sent", to, subj

    held_back_email: list[HotelFacts] = []
    for facts in facts_list:
        o = Outcome(hotel=facts.hotel, overall=facts.overall)
        result.outcomes.append(o)
        key = log_key(facts.hotel, facts.week_key, settings.mode)

        if not facts.email_valid:
            o.status, o.error = "held back", "owner email missing or invalid"
            held_back_email.append(facts)
            continue
        if key in already_sent and not force_resend:
            o.status = "skipped (already sent this week)"
            continue

        # ---- Step 3: write ------------------------------------------------
        others = [n for n in all_names if n != (facts.hotel_name_en or facts.hotel)]
        text, o.ai_status = write_summary(facts, generate, others)

        # ---- Step 4: send -------------------------------------------------
        try:
            o.status, o.sent_to, o.subject = deliver(
                facts.owner_email, text.subject,
                render_text(facts, text, settings.sender_name),
                render_html(facts, text, settings.sender_name),
                _slug(facts.hotel_name_en or facts.hotel),
            )
        except Exception as e:  # one bad send shouldn't stop the others
            o.status, o.error = "failed", f"{type(e).__name__}: {e}"

        result.log_entries.append({
            "log_key": key, "week_key": facts.week_key, "hotel": facts.hotel, "overall": facts.overall,
            "intended_recipient": facts.owner_email, "sent_to": o.sent_to, "mode": settings.mode,
            "ai_status": o.ai_status, "status": o.status, "subject": o.subject,
            "sent_at": now.isoformat(timespec="seconds"), "error": o.error,
        })

    # ---- Data checks ------------------------------------------------------
    # 0 revenue + 0 orders -> ask the owner to confirm (fixed wording, no AI).
    # Anything else (bad numbers, duplicates...) -> internal alert only.
    for r in data_checks:
        o = Outcome(hotel=r["hotel"], overall="Data check", ai_status="n/a", error=r["issues_text"])
        result.outcomes.append(o)
        if not (is_zero_activity(r) and r["email_valid"]):
            o.status = "held back"
            continue
        key = log_key(r["hotel"], r["week_key"], settings.mode)
        if key in already_sent and not force_resend:
            o.status = "skipped (already sent this week)"
            continue
        subject, text_body, html_body = zero_activity_email(r, settings.sender_name)
        try:
            o.status, o.sent_to, o.subject = deliver(
                r["owner_email"], subject, text_body, html_body, _slug(r["hotel_name_en"] or r["hotel"]))
        except Exception as e:
            o.status, o.error = "failed", f"{type(e).__name__}: {e}"
        result.log_entries.append({
            "log_key": key, "week_key": r["week_key"], "hotel": r["hotel"], "overall": "Data check",
            "intended_recipient": r["owner_email"], "sent_to": o.sent_to, "mode": settings.mode,
            "ai_status": "n/a", "status": o.status, "subject": o.subject,
            "sent_at": now.isoformat(timespec="seconds"), "error": o.error,
        })

    if data_checks or held_back_email:
        period, week = rows[0]["period_label"], rows[0]["week_key"]
        alert_key = log_key("_internal_alert", week, settings.mode)
        if alert_key not in already_sent or force_resend:
            subject, body = data_check_alert(data_checks, held_back_email, period)
            try:
                status, to, subj = deliver(settings.internal_email, subject, body, None, "internal-data-check-alert")
                err = ""
            except Exception as e:
                status, to, subj, err = "failed", "", subject, f"{type(e).__name__}: {e}"
            result.outcomes.append(Outcome(hotel="(internal alert)", overall="", status=status, sent_to=to, subject=subj, error=err))
            result.log_entries.append({
                "log_key": alert_key, "week_key": week, "hotel": "(internal alert)", "overall": "",
                "intended_recipient": settings.internal_email, "sent_to": to, "mode": settings.mode,
                "ai_status": "n/a", "status": status, "subject": subj,
                "sent_at": now.isoformat(timespec="seconds"), "error": err,
            })
    return result
