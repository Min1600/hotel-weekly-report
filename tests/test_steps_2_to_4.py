"""Tests for Steps 2–4 with a fake AI and a fake mail server. Run: python -m pytest -q"""
import json
from datetime import datetime
from pathlib import Path

import pytest

from hotel_report.classify import classify
from hotel_report.models import EmailText
from hotel_report.pipeline import run_pipeline
from hotel_report.pull import clean_rows, reporting_period
from hotel_report.send import SendSettings, render_html, route, zero_activity_email
from hotel_report.sheets import rows_from_csv, sent_keys_from_log
from hotel_report.write import build_prompt, check_text, template_text, write_summary

NOW = datetime.fromisoformat("2026-10-21T10:16:00+08:00")
ROWS = clean_rows(
    rows_from_csv((Path(__file__).parent / "mock_sheet.csv").read_text(encoding="utf-8")),
    reporting_period(NOW, "Asia/Kuala_Lumpur"), NOW,
)
FACTS, DATA_CHECKS, BM = classify(ROWS, "NT$")
F = {f.hotel_name_en: f for f in FACTS}
OTHERS = lambda name: [n for n in F if n != name] + ["Cape Cottage"]  # noqa: E731


def ai_reply(subject, summary):
    return json.dumps({"subject": subject, "summary": summary})


GOOD_ORANGE = ai_reply(
    "Orange Light · Weekly summary, Mon 12 – Sun 18 Oct 2026",
    "Orange Light finished below the partner benchmark this week, with revenue of NT$256,000, 19% below "
    "NT$315,000. Orders were 49 against a benchmark of 71, while average order value was NT$5,224, "
    "12% above the benchmark of NT$4,675.",
)


# --- Step 2: classify --------------------------------------------------------

def test_benchmarks_are_medians_of_active_hotels():
    assert (BM.revenue, BM.orders, BM.aov, BM.hotels_used) == (315000, 71, 4675, 9)


def test_zero_hotel_is_not_compared():
    assert [r["hotel_name_en"] for r in DATA_CHECKS] == ["Cape Cottage"]


@pytest.mark.parametrize("hotel, overall, verdicts", [
    ("Sky Spa Resort", "Doing well", ["D", "D", "D"]),
    ("Orange Light", "Needs improvement", ["N", "N", "D"]),
    ("Blue Bay Hostel", "Needs improvement", ["N", "N", "N"]),
    ("Riverside Hotel", "Doing well", ["D", "D", "N"]),   # exactly at the median = Doing well
    ("Starlight Biz", "Doing well", ["D", "D", "D"]),     # AOV exactly at the median
])
def test_verdicts(hotel, overall, verdicts):
    f = F[hotel]
    assert f.overall == overall
    assert [m.verdict[0] for m in f.metrics] == verdicts


def test_percentages():
    o = F["Orange Light"]
    assert [m.pct_vs_benchmark for m in o.metrics] == [-19, -31, 12]


def test_ai_only_sees_this_hotels_facts():
    shown = json.dumps(F["Songhe Inn"].for_ai(), ensure_ascii=False)
    assert "@" not in shown and "Sky Spa" not in shown and "482,000" in shown


# --- Step 3: write ------------------------------------------------------------

def test_good_ai_answer_is_used():
    text, status = write_summary(F["Orange Light"], lambda p: GOOD_ORANGE, OTHERS("Orange Light"))
    assert status == "ai" and text.summary.startswith("Orange Light")


def test_invented_number_triggers_retry_with_feedback():
    prompts = []
    replies = iter([GOOD_ORANGE.replace("NT$256,000", "NT$260,000"), GOOD_ORANGE])

    def gen(prompt):
        prompts.append(prompt)
        return next(replies)

    text, status = write_summary(F["Orange Light"], gen, OTHERS("Orange Light"))
    assert status == "ai (2nd try)"
    assert "260,000" in prompts[1] and "not in the facts" in prompts[1]


@pytest.mark.parametrize("bad_reply, reason", [
    (GOOD_ORANGE.replace("this week", "this week, and you should run a promotion"), "advice"),
    (GOOD_ORANGE.replace("this week", "this week, unlike Sky Spa Resort"), "another hotel"),
    (GOOD_ORANGE.replace("NT$256,000", "NT$260,000"), "numbers that are not in the facts"),
    (GOOD_ORANGE.replace("NT$256,000", "NT$256,000, about double last year"), "comparison that is not in the facts"),
    ("not json at all", "Invalid JSON"),
    (ai_reply("x" * 120, "too short"), "90"),
])
def test_bad_answers_fall_back_to_template(bad_reply, reason):
    text, status = write_summary(F["Orange Light"], lambda p: bad_reply, OTHERS("Orange Light"))
    assert status.startswith("template") and reason in status
    assert text == template_text(F["Orange Light"])


def test_api_error_falls_back_to_template():
    def boom(prompt):
        raise ConnectionError("quota exceeded")
    _, status = write_summary(F["Orange Light"], boom, OTHERS("Orange Light"))
    assert status.startswith("template") and "quota exceeded" in status


def test_no_api_key_uses_template():
    _, status = write_summary(F["Orange Light"], None, [])
    assert status == "template: no GEMINI_API_KEY set"


@pytest.mark.parametrize("hotel", list(F))
def test_template_passes_its_own_checks(hotel):
    assert check_text(template_text(F[hotel]), F[hotel], OTHERS(hotel)) == []


def test_prompt_contains_rules_and_facts():
    p = build_prompt(F["Orange Light"])
    assert "not advice" in p and '"this_week": "256,000"' in p and "19% below" in p


# --- Step 4: render + route ----------------------------------------------------

SETTINGS = SendSettings(test_mode=True, test_recipient="me@example.com", internal_email="me@example.com",
                        sender_name="Partner Success Team")


def test_test_mode_redirects_and_labels_subject():
    to, subj = route("owner@hotel.com", "Hello", SETTINGS)
    assert to == "me@example.com" and subj == "[TEST → owner@hotel.com] Hello"


def test_live_mode_goes_to_owner():
    live = SendSettings(test_mode=False, test_recipient="", internal_email="ops@x.com", sender_name="X")
    assert route("owner@hotel.com", "Hello", live) == ("owner@hotel.com", "Hello")


def test_table_is_rendered_by_code():
    html = render_html(F["Orange Light"], template_text(F["Orange Light"]), "Team")
    for s in ["NT$256,000", "NT$315,000", "19% below", "12% above", "Overall: Needs improvement"]:
        assert s in html


def test_zero_activity_email_asks_owner():
    subject, text, html = zero_activity_email(DATA_CHECKS[0], "Team")
    assert "0 orders and 0 revenue" in text and "closed" in text and "no guests completed a stay" in text
    assert "Cape Cottage" in subject


# --- Full pipeline ------------------------------------------------------------

class FakeSender:
    def __init__(self, fail_for=None):
        self.sent, self.fail_for = [], fail_for

    def send(self, msg):
        if self.fail_for and self.fail_for in msg["Subject"]:
            raise OSError("SMTP down")
        self.sent.append(msg)


def pipeline(tmp_path, **kw):
    args = dict(currency="NT$", settings=SETTINGS, generate=None, sender=FakeSender(), now=NOW, out_dir=tmp_path)
    args.update(kw)
    return run_pipeline(ROWS, **args), args["sender"]


def test_everything_goes_to_test_inbox_only(tmp_path):
    result, sender = pipeline(tmp_path)
    # 9 summaries + 1 zero-activity check to Cape Cottage + 1 internal alert
    assert len(sender.sent) == 11
    assert {m["To"] for m in sender.sent} == {"me@example.com"}
    songhe = [m for m in sender.sent if "Songhe" in m["Subject"]][0]
    assert "owner.songhe@example.com" in songhe["Subject"] # shows who it was meant for
    assert not result.failed


def test_rerun_skips_hotels_already_sent(tmp_path):
    first, _ = pipeline(tmp_path)
    sent = {e["log_key"] for e in first.log_entries if e["status"] == "sent"}
    second, sender = pipeline(tmp_path, sender=FakeSender(), already_sent=sent)
    assert sender.sent == []
    assert sum(o.status.startswith("skipped") for o in second.outcomes) == 10


def test_force_resend(tmp_path):
    first, _ = pipeline(tmp_path)
    sent = {e["log_key"] for e in first.log_entries}
    _, sender = pipeline(tmp_path, sender=FakeSender(), already_sent=sent, force_resend=True)
    assert len(sender.sent) == 11


def test_test_sends_dont_block_live_sends(tmp_path):
    first, _ = pipeline(tmp_path)
    sent = {e["log_key"] for e in first.log_entries}
    live = SendSettings(test_mode=False, test_recipient="", internal_email="ops@x.com", sender_name="X")
    _, sender = pipeline(tmp_path, sender=FakeSender(), already_sent=sent, settings=live)
    assert len(sender.sent) == 11


def test_one_failed_send_doesnt_stop_the_rest(tmp_path):
    result, sender = pipeline(tmp_path, sender=FakeSender(fail_for="Ocean View"))
    assert result.failed and len(sender.sent) == 10
    assert [o.status for o in result.outcomes if o.hotel.endswith("Ocean View")] == ["failed"]


def test_preview_mode_sends_nothing_but_writes_files(tmp_path):
    result, _ = pipeline(tmp_path, sender=None)
    assert all(o.status in ("preview",) for o in result.outcomes)
    assert (tmp_path / "orange-light.html").exists() and (tmp_path / "cape-cottage.html").exists()


def test_invalid_owner_email_held_back_and_reported(tmp_path):
    rows = [dict(r) for r in ROWS]
    rows[2] = {**rows[2], "owner_email": "", "email_valid": False}   # Blue Bay
    result = run_pipeline(rows, currency="NT$", settings=SETTINGS, generate=None, sender=FakeSender(),
                          now=NOW, out_dir=tmp_path)
    bb = [o for o in result.outcomes if "Blue Bay" in o.hotel][0]
    assert bb.status == "held back"
    alert = (tmp_path / "internal-data-check-alert.eml").read_bytes().decode()
    assert "Blue Bay Hostel" in alert and "missing or invalid" in alert


def test_ai_used_in_pipeline(tmp_path):
    def gen(prompt):
        return GOOD_ORANGE if '"hotel": "Orange Light"' in prompt else "nope"
    result, _ = pipeline(tmp_path, generate=gen)
    statuses = {o.hotel: o.ai_status for o in result.outcomes}
    assert statuses["橙光設計旅店 Orange Light"] == "ai"
    assert statuses["晴空溫泉會館 Sky Spa Resort"].startswith("template")


def test_run_log_reader():
    log = [["log_key", "status"], ["a|W42|test", "sent"], ["b|W42|test", "failed"], ["c|W42|test", "preview"]]
    assert sent_keys_from_log(log) == {"a|W42|test"}
