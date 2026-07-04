from __future__ import annotations

from datetime import datetime

from flask import Flask

from finance.blueprints.accounts import accounts_bp
from finance.blueprints.dashboard import dashboard_bp
from finance.blueprints.desk import desk_bp
from finance.blueprints.forecast import forecast_bp
from finance.blueprints.plaid import plaid_bp
from finance.blueprints.rules import normalize_merchant, rules_bp
from finance.blueprints.transactions import transactions_bp


def register_blueprints(app: Flask) -> None:
    """Register all blueprints and shared template filters."""

    @app.context_processor
    def inject_dateline():
        """Masthead dateline, e.g. 'Thursday · July 3 · 2026' (uppercased by CSS)."""
        now = datetime.now()
        return {"dateline": f"{now:%A} · {now:%B} {now.day} · {now.year}"}

    @app.template_filter("currency")
    def currency_filter(value):
        if value is None:
            return "N/A"
        try:
            value = float(value)
        except (TypeError, ValueError):
            return str(value)
        if value < 0:
            return f"-${abs(value):,.2f}"
        return f"${value:,.2f}"

    @app.template_filter("to_short_date")
    def to_short_date_filter(value):
        """'2026-06-29' -> 'Jun 29'"""
        if not value:
            return ""
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%b %d")
        except (TypeError, ValueError):
            return str(value)

    @app.template_filter("normalize_merchant")
    def normalize_merchant_filter(value):
        """Merchant grouping key (see finance.blueprints.rules.normalize_merchant), for
        pre-filling the "Categorize Similar" keyword field from a transaction description."""
        return normalize_merchant(value)

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(transactions_bp)
    app.register_blueprint(rules_bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(forecast_bp)
    app.register_blueprint(plaid_bp)
    app.register_blueprint(desk_bp)
