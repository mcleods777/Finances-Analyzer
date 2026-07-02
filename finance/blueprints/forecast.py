from __future__ import annotations

import logging
from datetime import date

from flask import Blueprint, jsonify, render_template, request

from finance.data_service import get_cache
from finance.forecast import (
    DEFAULT_HORIZON_DAYS,
    clamp_horizon,
    derive_paycheck_amount,
    project_cash_flow,
)

logger = logging.getLogger(__name__)

forecast_bp = Blueprint("forecast", __name__)

# Same "spendable cash" notion as the runway calc in data_service.refresh_data:
# checking + savings only — manual_balance/investment holdings and credit
# cards/loans aren't near-term spending money for a cash-flow projection.
CASH_ACCOUNT_TYPES = {"checking", "savings"}


def _current_cash_balance() -> float:
    """Latest total balance across checking + savings accounts."""
    daily_bal = get_cache().get("daily_balances")
    if daily_bal is None or daily_bal.empty:
        return 0.0
    latest = daily_bal.groupby("account_name").last()
    total = 0.0
    for _, row in latest.iterrows():
        if row["account_type"] in CASH_ACCOUNT_TYPES:
            total += row["balance"]
    return round(total, 2)


def _empty_forecast_response(horizon_days: int) -> dict:
    today_iso = date.today().isoformat()
    return {
        "starting_balance": 0,
        "as_of": today_iso,
        "horizon_days": horizon_days,
        "days": [],
        "monthly": [],
        "warnings": {
            "goes_negative": False,
            "first_negative_date": None,
            "min_balance": 0,
            "min_balance_date": today_iso,
        },
    }


@forecast_bp.route("/forecast")
def forecast_page():
    """Calendar cash-flow forecast page — data is fetched client-side from /api/forecast."""
    return render_template("forecast.html")


@forecast_bp.route("/api/forecast")
def api_forecast():
    """
    Project cash flow forward from today.

    Query params:
        horizon: number of days to project (clamped to [1, 365], default 90)
    """
    horizon_param = request.args.get("horizon", DEFAULT_HORIZON_DAYS)

    _cache = get_cache()
    config = _cache.get("config")
    if config is None:
        return jsonify(_empty_forecast_response(clamp_horizon(horizon_param)))

    current_balance = _current_cash_balance()
    paycheck_amount = derive_paycheck_amount(_cache.get("biweekly_income_df"))

    result = project_cash_flow(
        current_balance=current_balance,
        pay_period=config.pay_period,
        paycheck_amount=paycheck_amount,
        recurring_bills=config.recurring_bills,
        temporary_expenses=config.temporary_expenses,
        start_date=date.today(),
        horizon_days=horizon_param,
    )
    return jsonify(result)
