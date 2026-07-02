from __future__ import annotations

import logging

from flask import Blueprint, render_template

logger = logging.getLogger(__name__)

forecast_bp = Blueprint("forecast", __name__)


@forecast_bp.route("/forecast")
def forecast_page():
    """Calendar cash-flow forecast — placeholder, implemented in a later wave."""
    return render_template("forecast.html")
