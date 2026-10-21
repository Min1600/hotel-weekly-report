"""Step 2 · Compare each hotel with this week's benchmark.

Rules (kept deliberately simple, because we only have one week of data):
  * Benchmark = median of the hotels whose data passed Step 1's checks.
    Median rather than mean so one very large hotel doesn't move it.
  * For each of revenue, orders and average order value (revenue ÷ orders):
        at or above the benchmark -> "Doing well"
        below the benchmark       -> "Needs improvement"
  * The hotel's overall verdict = its revenue verdict (the outcome owners
    care about most; orders × AOV make up revenue).
  * Hotels flagged in Step 1 ("data_check") are not compared at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from .models import HotelFacts, Metric


@dataclass
class Benchmarks:
    revenue: float
    orders: float
    aov: float
    hotels_used: int


def compute_benchmarks(rows: list[dict]) -> Benchmarks:
    active = [r for r in rows if r["status"] == "ok"]
    if not active:
        raise ValueError("No hotels passed the data checks, so there is nothing to compare against.")
    return Benchmarks(
        revenue=median(r["revenue"] for r in active),
        orders=median(r["orders"] for r in active),
        aov=median(r["revenue"] / r["orders"] for r in active),
        hotels_used=len(active),
    )


def _metric(name, value: float, benchmark: float, is_money: bool) -> Metric:
    v, b = round(value), round(benchmark)
    return Metric(
        name=name,
        value=v,
        benchmark=b,
        pct_vs_benchmark=round((value - benchmark) / benchmark * 100),
        # compare the rounded figures the owner will see, so the table never
        # shows "4,675 vs 4,675 · Needs improvement"
        verdict="Doing well" if v >= b else "Needs improvement",
        is_money=is_money,
    )


def classify(rows: list[dict], currency: str) -> tuple[list[HotelFacts], list[dict], Benchmarks]:
    """Returns (facts for hotels to report on, rows needing a data check, benchmarks)."""
    bm = compute_benchmarks(rows)
    facts, data_checks = [], []
    for r in rows:
        if r["status"] != "ok":
            data_checks.append(r)
            continue
        metrics = [
            _metric("Revenue", r["revenue"], bm.revenue, True),
            _metric("Orders", r["orders"], bm.orders, False),
            _metric("Avg order value", r["revenue"] / r["orders"], bm.aov, True),
        ]
        facts.append(HotelFacts(
            hotel=r["hotel"],
            hotel_name_en=r["hotel_name_en"],
            period_label=r["period_label"],
            week_key=r["week_key"],
            currency=currency,
            overall=metrics[0].verdict,
            metrics=metrics,
            owner_email=r["owner_email"],
            email_valid=r["email_valid"],
        ))
    return facts, data_checks, bm
