"""Typed data passed between the steps.

HotelFacts  : Step 2 -> Step 3. The only thing the AI is shown.
EmailText   : what the AI must return (also used as Gemini's response schema).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Verdict = Literal["Doing well", "Needs improvement"]
MetricName = Literal["Revenue", "Orders", "Avg order value"]


class Metric(BaseModel):
    name: MetricName
    value: int                    # this hotel, this week (rounded to whole numbers)
    benchmark: int                # median of active hotels this week
    pct_vs_benchmark: int         # -19 means 19% below the benchmark
    verdict: Verdict              # at or above the benchmark = Doing well
    is_money: bool                # shown with the currency symbol

    @property
    def difference_text(self) -> str:   # "19% below", "12% above", "level with"
        p = self.pct_vs_benchmark
        if p == 0:
            return "level with"
        return f"{abs(p)}% {'above' if p > 0 else 'below'}"


class HotelFacts(BaseModel):
    hotel: str
    hotel_name_en: str
    period_label: str             # "Mon 12 – Sun 18 Oct 2026"
    week_key: str                 # "2026-W42"
    currency: str                 # "NT$"
    overall: Verdict              # set by the revenue verdict
    metrics: list[Metric] = Field(min_length=3, max_length=3)
    owner_email: str
    email_valid: bool

    def metric(self, name: MetricName) -> Metric:
        return next(m for m in self.metrics if m.name == name)

    def for_ai(self) -> dict:
        """Just the facts the AI needs: no email address, no other hotels."""
        return {
            "hotel": self.hotel_name_en or self.hotel,
            "period": self.period_label,
            "currency": self.currency,
            "overall": self.overall,
            "metrics": [
                {
                    "name": m.name,
                    "this_week": f"{m.value:,}",
                    "benchmark": f"{m.benchmark:,}",
                    "difference": m.difference_text,
                    "verdict": m.verdict,
                }
                for m in self.metrics
            ],
        }


class EmailText(BaseModel):
    """The AI writes these two fields; code builds everything else in the email."""

    subject: str = Field(description="Email subject line, under 90 characters.")
    summary: str = Field(description="2-3 sentence factual summary of the week. No advice.")

    @field_validator("subject")
    @classmethod
    def subject_short(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 90:
            raise ValueError("subject must be 1-90 characters")
        return v

    @field_validator("summary")
    @classmethod
    def summary_short(cls, v: str) -> str:
        v = v.strip()
        words = len(v.split())
        if words < 10 or words > 80:
            raise ValueError(f"summary must be 10-80 words (got {words})")
        return v
