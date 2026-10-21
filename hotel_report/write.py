"""Step 3 · Turn each hotel's facts into a short, friendly summary.

Design: code owns the numbers, the AI owns the wording.
  1. The AI sees only this hotel's HotelFacts (no other hotels, no email address).
  2. Gemini must answer in the EmailText schema (Pydantic validates it).
  3. Every number it writes must appear in the facts, it must not give advice,
     and it must not name another hotel. Otherwise: retry once with the reason,
     then fall back to a fixed template. The result records which path was used.
"""
from __future__ import annotations

import json
import re
from typing import Callable

from pydantic import ValidationError

from .models import EmailText, HotelFacts

SYSTEM_INSTRUCTIONS = """\
You write the weekly performance summary that a hotel booking platform emails
to one partner hotel every Monday.

Write two things:
- subject: a clear subject line under 90 characters that includes the hotel name
  and the reporting week.
- summary: 2-3 short sentences (under 70 words) describing how the hotel did.

Rules:
- Be factual and warm, like a helpful account manager. Plain English, no jargon.
- Start with the overall result, then mention revenue, orders and average order
  value, each compared with the benchmark.
- "Benchmark" means the median of partner hotels this week. Call it "the partner
  benchmark" or "the benchmark".
- Use ONLY numbers that appear in the facts, written exactly as given
  (e.g. "256,000", "19%"). Put the currency symbol before money amounts.
  Don't calculate new numbers, round, or use words like "double" or "half".
- You only have this one week. Don't compare with previous weeks or years.
- This is a summary, not advice: don't recommend actions or say what the hotel
  should do.
- Don't mention any other hotel. Don't use emojis.
"""

ADVICE_WORDS = re.compile(
    r"\b(recommend\w*|suggest\w*|should|consider|we advise|try to|you could|you might want)\b", re.I
)
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Comparisons written in words. The facts only cover one week against the
# benchmark, so any claim about trends or past periods is invented.
UNSUPPORTED_CLAIMS = re.compile(
    r"\b(doubled?|tripled?|halved?|twice|half|last (?:year|month)|previous (?:week|month|year)|"
    r"week[- ]on[- ]week|year[- ]on[- ]year|compared (?:to|with) (?:last|previous)|record|best ever|worst)\b",
    re.I,
)


def build_prompt(facts: HotelFacts, feedback: str | None = None) -> str:
    prompt = SYSTEM_INSTRUCTIONS + "\nFacts (JSON):\n" + json.dumps(facts.for_ai(), ensure_ascii=False, indent=2)
    if feedback:
        prompt += f"\n\nYour previous answer was rejected: {feedback}\nWrite it again following every rule."
    return prompt


def allowed_numbers(facts: HotelFacts) -> set[str]:
    allowed = set(NUMBER.findall(facts.period_label)) | set(NUMBER.findall(facts.hotel))
    for m in facts.metrics:
        allowed |= {str(m.value), str(m.benchmark), str(abs(m.pct_vs_benchmark))}
    return {a.replace(",", "") for a in allowed}


def check_text(text: EmailText, facts: HotelFacts, other_hotels: list[str]) -> list[str]:
    """Problems Pydantic can't see: invented numbers, advice, other hotels."""
    problems = []
    body = f"{text.subject}\n{text.summary}"
    allowed = allowed_numbers(facts)
    bad = sorted({n for n in NUMBER.findall(body) if n.replace(",", "").rstrip(".") not in allowed})
    if bad:
        problems.append(f"it used numbers that are not in the facts: {', '.join(bad)}")
    if m := UNSUPPORTED_CLAIMS.search(body):
        problems.append(f'it made a comparison that is not in the facts ("{m.group(0)}")')
    if m := ADVICE_WORDS.search(body):
        problems.append(f'it gave advice ("{m.group(0)}"); only summarise')
    for other in other_hotels:
        if other and other.lower() in body.lower():
            problems.append(f"it mentioned another hotel ({other})")
    name = (facts.hotel_name_en or facts.hotel).lower()
    if name not in body.lower():
        problems.append("it did not mention the hotel's name")
    return problems


def template_text(facts: HotelFacts) -> EmailText:
    """Fixed wording used when the AI is unavailable or fails the checks."""
    rev, orders, aov = (facts.metric(n) for n in ("Revenue", "Orders", "Avg order value"))
    cur = facts.currency
    name = facts.hotel_name_en or facts.hotel
    overall = "had a strong week" if facts.overall == "Doing well" else "had a below-benchmark week"
    summary = (
        f"{name} {overall} ({facts.period_label}). "
        f"Revenue was {cur}{rev.value:,}, {rev.difference_text} the partner benchmark of {cur}{rev.benchmark:,}. "
        f"Orders were {orders.value:,} against a benchmark of {orders.benchmark:,}, "
        f"and average order value was {cur}{aov.value:,} against {cur}{aov.benchmark:,}."
    )
    return EmailText(subject=f"{name} · Weekly performance summary ({facts.period_label})", summary=summary)


def write_summary(
    facts: HotelFacts,
    generate: Callable[[str], str] | None,
    other_hotels: list[str],
    attempts: int = 2,
) -> tuple[EmailText, str]:
    """Returns (text, ai_status). ai_status is "ai", "ai (2nd try)" or "template: <reason>"."""
    if generate is None:
        return template_text(facts), "template: no GEMINI_API_KEY set"

    feedback = None
    for attempt in range(1, attempts + 1):
        try:
            raw = generate(build_prompt(facts, feedback))
            text = EmailText.model_validate_json(raw)
        except ValidationError as e:
            feedback = "; ".join(err["msg"] for err in e.errors())
            continue
        except Exception as e:  # network, quota, safety block...
            feedback = f"{type(e).__name__}: {e}"
            continue
        problems = check_text(text, facts, other_hotels)
        if not problems:
            return text, "ai" if attempt == 1 else "ai (2nd try)"
        feedback = "; ".join(problems)
    return template_text(facts), f"template: AI failed checks ({feedback})"


def gemini_generator(api_key: str, model: str) -> Callable[[str], str]:
    """Real Gemini call. Returns JSON text that matches the EmailText schema."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=EmailText,
        temperature=0.4,
    )

    def generate(prompt: str) -> str:
        resp = client.models.generate_content(model=model, contents=prompt, config=config)
        if not resp.text:
            raise RuntimeError("empty response from Gemini")
        return resp.text

    return generate
