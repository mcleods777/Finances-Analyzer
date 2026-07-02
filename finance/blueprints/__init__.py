from __future__ import annotations

from flask import Flask

from finance.blueprints.accounts import accounts_bp
from finance.blueprints.dashboard import dashboard_bp
from finance.blueprints.forecast import forecast_bp
from finance.blueprints.rules import rules_bp
from finance.blueprints.transactions import transactions_bp


def register_blueprints(app: Flask) -> None:
    """Register all blueprints and shared template filters."""

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

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(transactions_bp)
    app.register_blueprint(rules_bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(forecast_bp)
