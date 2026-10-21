"""Step 4 · Build the email and send it.

Safety:
  * TEST_MODE is on unless it is explicitly set to "false". In test mode every
    email goes to TEST_RECIPIENT, and the subject shows who it was meant for.
  * DRY_RUN (or missing Gmail credentials) writes previews to output/emails/
    instead of sending anything.
  * Every send is recorded in a "Run Log" tab. A hotel already logged as sent
    for this week (in this mode) is skipped, so a re-run never double-emails.
"""
from __future__ import annotations

import html
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .models import EmailText, HotelFacts

RUN_LOG_COLUMNS = [
    "log_key", "week_key", "hotel", "overall", "intended_recipient", "sent_to",
    "mode", "ai_status", "status", "subject", "sent_at", "error",
]


@dataclass
class SendSettings:
    test_mode: bool
    test_recipient: str
    internal_email: str
    sender_name: str
    gmail_address: str = ""
    gmail_app_password: str = field(default="", repr=False)
    dry_run: bool = False

    @property
    def can_send(self) -> bool:
        return not self.dry_run and bool(self.gmail_address and self.gmail_app_password)

    @property
    def mode(self) -> str:
        return "test" if self.test_mode else "live"


# ---------------------------------------------------------------------------
# Rendering (the figures table is built here, never by the AI)
# ---------------------------------------------------------------------------

GREEN, RED, GREY = "#1a7f37", "#b42318", "#667085"


def _fmt(value: int, money: bool, cur: str) -> str:
    return f"{cur}{value:,}" if money else f"{value:,}"


def render_html(facts: HotelFacts, text: EmailText, sender_name: str) -> str:
    esc = html.escape
    rows = []
    for m in facts.metrics:
        colour = GREEN if m.verdict == "Doing well" else RED
        arrow = "▲" if m.pct_vs_benchmark > 0 else "▼" if m.pct_vs_benchmark < 0 else "＝"
        rows.append(
            "<tr>"
            f'<td style="padding:10px 12px;border-bottom:1px solid #eaecf0">{esc(m.name)}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #eaecf0;text-align:right;font-weight:600">{_fmt(m.value, m.is_money, facts.currency)}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #eaecf0;text-align:right;color:{GREY}">{_fmt(m.benchmark, m.is_money, facts.currency)}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #eaecf0;color:{colour}">{arrow} {esc(m.difference_text)} · {esc(m.verdict)}</td>'
            "</tr>"
        )
    badge = GREEN if facts.overall == "Doing well" else RED
    name = esc(facts.hotel)
    return f"""<!doctype html>
<html><body style="margin:0;background:#f5f6f8;font-family:Arial,Helvetica,sans-serif;color:#101828">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f6f8;padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:8px;overflow:hidden">
  <tr><td style="padding:24px 24px 8px">
    <div style="font-size:13px;color:{GREY}">Weekly performance summary · {esc(facts.period_label)}</div>
    <h1 style="font-size:20px;margin:6px 0 12px">{name}</h1>
    <span style="display:inline-block;padding:4px 10px;border-radius:999px;background:{badge};color:#fff;font-size:13px">Overall: {esc(facts.overall)}</span>
  </td></tr>
  <tr><td style="padding:12px 24px;font-size:15px;line-height:1.55">{esc(text.summary)}</td></tr>
  <tr><td style="padding:4px 24px 8px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:14px">
      <tr style="background:#f9fafb;color:{GREY};font-size:12px;text-transform:uppercase">
        <th align="left" style="padding:8px 12px">Metric</th>
        <th align="right" style="padding:8px 12px">This week</th>
        <th align="right" style="padding:8px 12px">Benchmark</th>
        <th align="left" style="padding:8px 12px">vs benchmark</th>
      </tr>
      {''.join(rows)}
    </table>
  </td></tr>
  <tr><td style="padding:12px 24px 24px;font-size:12px;color:{GREY};line-height:1.5">
    Benchmark = the median of partner hotels on the platform this week.
    At or above the benchmark counts as "Doing well".<br>— {esc(sender_name)}
  </td></tr>
</table>
</td></tr></table>
</body></html>"""


def render_text(facts: HotelFacts, text: EmailText, sender_name: str) -> str:
    lines = [
        f"{facts.hotel} · Weekly performance summary · {facts.period_label}",
        f"Overall: {facts.overall}",
        "",
        text.summary,
        "",
    ]
    for m in facts.metrics:
        lines.append(
            f"- {m.name}: {_fmt(m.value, m.is_money, facts.currency)} "
            f"(benchmark {_fmt(m.benchmark, m.is_money, facts.currency)}, {m.difference_text}) · {m.verdict}"
        )
    lines += ["", "Benchmark = the median of partner hotels on the platform this week.", f"— {sender_name}"]
    return "\n".join(lines)


def is_zero_activity(row: dict) -> bool:
    """0 revenue and 0 orders: worth asking the owner, not just the internal team."""
    return row.get("revenue") == 0 and row.get("orders") == 0


def zero_activity_email(row: dict, sender_name: str) -> tuple[str, str, str]:
    """Owner email for a week with no bookings and no revenue (fixed wording, no AI)."""
    name = row["hotel_name_en"] or row["hotel"]
    period = row["period_label"]
    subject = f"{name} · Quick check on your figures for {period}"
    options = [
        "The property was closed for part or all of the week (for example, maintenance or renovation).",
        "The property was open, but no guests completed a stay.",
        "Neither. You did have bookings, so our data may be incomplete.",
    ]
    text = "\n".join([
        f"Hi {name} team,",
        "",
        f"Our records show 0 orders and 0 revenue for {row['hotel']} for {period}.",
        "Before we send your weekly performance summary, could you reply and tell us which of these applies?",
        "",
        *[f"{i}. {o}" for i, o in enumerate(options, 1)],
        "",
        "Once we hear back, we'll send your summary or correct the figures.",
        "",
        f"— {sender_name}",
    ])
    esc = html.escape
    items = "".join(f'<li style="margin:0 0 6px">{esc(o)}</li>' for o in options)
    html_body = f"""<!doctype html>
<html><body style="margin:0;background:#f5f6f8;font-family:Arial,Helvetica,sans-serif;color:#101828">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f5f6f8;padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:8px">
  <tr><td style="padding:24px;font-size:15px;line-height:1.55">
    <div style="font-size:13px;color:{GREY}">Weekly performance summary · {esc(period)}</div>
    <h1 style="font-size:20px;margin:6px 0 16px">{esc(row['hotel'])}</h1>
    <p style="margin:0 0 12px">Hi {esc(name)} team,</p>
    <p style="margin:0 0 12px">Our records show <b>0 orders</b> and <b>0 revenue</b> for {esc(period)}.
    Before we send your weekly performance summary, could you reply and tell us which of these applies?</p>
    <ol style="margin:0 0 12px;padding-left:20px">{items}</ol>
    <p style="margin:0 0 12px">Once we hear back, we'll send your summary or correct the figures.</p>
    <p style="margin:0;color:{GREY};font-size:13px">— {esc(sender_name)}</p>
  </td></tr>
</table></td></tr></table></body></html>"""
    return subject, text, html_body


def data_check_alert(rows: list[dict], invalid_email: list[HotelFacts], period_label: str) -> tuple[str, str]:
    """Internal email listing hotels whose report was held back."""
    lines = [f"These hotels did not get a report for {period_label}. Please check the data.", ""]
    for r in rows:
        note = " (owner emailed to confirm)" if is_zero_activity(r) and r["email_valid"] else ""
        lines.append(f"- {r['hotel']} (sheet row {r['source_row']}): {r['issues_text']}{note}")
    for f in invalid_email:
        lines.append(f"- {f.hotel}: report is ready, but the owner email \"{f.owner_email}\" is missing or invalid")
    return f"[Action needed] {len(rows) + len(invalid_email)} hotel(s) held back · {period_label}", "\n".join(lines)


# ---------------------------------------------------------------------------
# Routing + sending
# ---------------------------------------------------------------------------

def route(intended: str, subject: str, s: SendSettings) -> tuple[str, str]:
    """(actual recipient, subject). Test mode always redirects to TEST_RECIPIENT."""
    if s.test_mode:
        return s.test_recipient, f"[TEST → {intended or 'no owner email'}] {subject}"
    return intended, subject


def build_message(to: str, subject: str, text_body: str, html_body: str | None, s: SendSettings) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((s.sender_name, s.gmail_address or "no-reply@example.com"))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return msg


class GmailSender:
    """One SMTP connection for the whole run. Gmail needs an App Password."""

    def __init__(self, s: SendSettings, host: str = "smtp.gmail.com", port: int = 465):
        self.s, self.host, self.port, self.conn = s, host, port, None

    def __enter__(self):
        self.conn = smtplib.SMTP_SSL(self.host, self.port, context=ssl.create_default_context(), timeout=30)
        self.conn.login(self.s.gmail_address, self.s.gmail_app_password)
        return self

    def send(self, msg: EmailMessage) -> None:
        self.conn.send_message(msg)

    def __exit__(self, *exc):
        if self.conn:
            self.conn.quit()


def log_key(hotel: str, week_key: str, mode: str) -> str:
    return f"{hotel}|{week_key}|{mode}"
