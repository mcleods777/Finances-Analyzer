from __future__ import annotations

import logging

import yaml
from flask import Blueprint, jsonify, render_template, request

from finance.analytics import get_category_trends, get_spending_breakdown
from finance.data_service import get_cache, get_config_path, get_data_dir, refresh_data
from finance.manual_balances import get_manual_account_names

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def dashboard():
    _cache = get_cache()
    if "error" in _cache:
        return render_template(
            "dashboard.html", summary=None, runway=None,
            manual_accounts=[], error=_cache["error"],
        )
    summary = _cache.get("summary", {})
    runway = _cache.get("runway", {})
    manual_accounts = get_manual_account_names(get_data_dir())
    return render_template(
        "dashboard.html", summary=summary, runway=runway,
        manual_accounts=manual_accounts, error=None,
    )


@dashboard_bp.route("/api/net-worth")
def api_net_worth():
    nw = get_cache().get("net_worth_series")
    if nw is None or nw.empty:
        return jsonify({"labels": [], "datasets": []})

    labels = [d.strftime("%Y-%m-%d") for d in nw["date"]]

    datasets = [
        {
            "label": "Net Worth",
            "data": [round(float(v), 2) for v in nw["net_worth"]],
        }
    ]

    # Add per-account lines
    for col in nw.columns:
        if col in ("date", "net_worth"):
            continue
        datasets.append(
            {
                "label": col,
                "data": [round(float(v), 2) for v in nw[col]],
            }
        )

    return jsonify({"labels": labels, "datasets": datasets})


@dashboard_bp.route("/api/biweekly-spending")
def api_biweekly_spending():
    _cache = get_cache()
    bw = _cache.get("biweekly_df")
    avg = _cache.get("avg_stats", {})

    if bw is None or bw.empty:
        return jsonify({"labels": [], "spending": [], "average": 0, "rolling_average": []})

    labels = []
    period_starts = []
    period_ends = []

    for _, row in bw.iterrows():
        start = row["period_start"]
        end = row["period_end"]
        labels.append(f"{start.strftime('%b %d')}-{end.strftime('%b %d')}")
        period_starts.append(start.strftime("%Y-%m-%d"))
        period_ends.append(end.strftime("%Y-%m-%d"))

    return jsonify(
        {
            "labels": labels,
            "period_starts": period_starts,
            "period_ends": period_ends,
            "spending": [round(float(v), 2) for v in bw["total_spending"]],
            "average": avg.get("overall_average", 0),
            "rolling_average": avg.get("rolling_average", []),
        }
    )


@dashboard_bp.route("/api/biweekly-income")
def api_biweekly_income():
    bw = get_cache().get("biweekly_income_df")

    if bw is None or bw.empty:
        return jsonify({"labels": [], "income": [], "period_starts": [], "period_ends": []})

    labels = []
    period_starts = []
    period_ends = []

    for _, row in bw.iterrows():
        start = row["period_start"]
        end = row["period_end"]
        labels.append(f"{start.strftime('%b %d')}-{end.strftime('%b %d')}")
        period_starts.append(start.strftime("%Y-%m-%d"))
        period_ends.append(end.strftime("%Y-%m-%d"))

    return jsonify(
        {
            "labels": labels,
            "period_starts": period_starts,
            "period_ends": period_ends,
            "income": [round(float(v), 2) for v in bw["total_income"]],
        }
    )


@dashboard_bp.route("/api/runway")
def api_runway():
    return jsonify(get_cache().get("runway", {}))


@dashboard_bp.route("/api/summary")
def api_summary():
    return jsonify(get_cache().get("summary", {}))


@dashboard_bp.route("/api/spending-breakdown")
def api_spending_breakdown():
    days_param = request.args.get("days", "30")
    try:
        days = int(days_param)
    except ValueError:
        days = 30

    df = get_cache().get("df")
    if df is None:
        return jsonify([])

    breakdown = get_spending_breakdown(df, days)
    return jsonify(breakdown)


@dashboard_bp.route("/api/category-trends")
def api_category_trends():
    categories_param = request.args.get("categories", "")
    categories = [c.strip() for c in categories_param.split(",") if c.strip()]
    months_param = request.args.get("months", "12")
    try:
        months = int(months_param)
    except ValueError:
        months = 12

    df = get_cache().get("df")
    if df is None:
        return jsonify({"labels": [], "datasets": {}})

    # If no categories specified, use top subcategories by total spending
    if not categories:
        expenses = df[df["category"] == "expense"].copy()
        if expenses.empty:
            return jsonify({"labels": [], "datasets": {}})
        expenses["subcategory"] = expenses["subcategory"].fillna("Uncategorized")
        expenses.loc[expenses["subcategory"] == "", "subcategory"] = "Uncategorized"
        top = (
            expenses.groupby("subcategory")["amount"]
            .agg(lambda x: x.abs().sum())
            .sort_values(ascending=False)
            .head(8)
            .index.tolist()
        )
        categories = top

    return jsonify(get_category_trends(df, categories, months))


@dashboard_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    try:
        refresh_data()
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.exception("Failed to refresh data")
        return jsonify({"status": "error", "message": str(e)}), 500


# --- Monthly Runway & Budget Simulator API ---


@dashboard_bp.route("/api/monthly-runway")
def api_monthly_runway():
    return jsonify(get_cache().get("monthly_runway", {}))


@dashboard_bp.route("/api/budget-overrides", methods=["POST"])
def api_save_budget_overrides():
    """
    Save budget overrides for monthly halves.
    Body: { "first_half": 1500.00, "second_half": null }
    """
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400

    try:
        config_path = get_config_path()

        with open(config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        if "budget_overrides" not in config_data:
            config_data["budget_overrides"] = {}

        # Update only the fields that were sent
        if "first_half" in data:
            val = data["first_half"]
            config_data["budget_overrides"]["first_half"] = float(val) if val is not None else None
        if "second_half" in data:
            val = data["second_half"]
            config_data["budget_overrides"]["second_half"] = float(val) if val is not None else None

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, sort_keys=False)

        refresh_data()
        return jsonify({"status": "ok"})

    except Exception as e:
        logger.exception("Failed to save budget overrides")
        return jsonify({"status": "error", "message": str(e)}), 500


@dashboard_bp.route("/api/temporary-expenses", methods=["POST"])
def api_save_temporary_expense():
    """
    Add a temporary expense.
    Body: { "name": "2nd Rent", "amount": 800, "half": 1 }
    """
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "No JSON body"}), 400

    name = data.get("name", "").strip()
    try:
        amount = float(data.get("amount", 0))
        half = int(data.get("half", 1))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid amount or half"}), 400

    if not name or amount <= 0 or half not in (1, 2):
        return jsonify({"status": "error", "message": "Name, positive amount, and half (1 or 2) are required"}), 400

    try:
        config_path = get_config_path()

        with open(config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        if "temporary_expenses" not in config_data or not config_data["temporary_expenses"]:
            config_data["temporary_expenses"] = []

        config_data["temporary_expenses"].append({
            "name": name,
            "amount": amount,
            "half": half,
        })

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, sort_keys=False)

        refresh_data()
        return jsonify({"status": "ok"})

    except Exception as e:
        logger.exception("Failed to save temporary expense")
        return jsonify({"status": "error", "message": str(e)}), 500


@dashboard_bp.route("/api/temporary-expenses", methods=["DELETE"])
def api_delete_temporary_expense():
    """
    Delete a temporary expense by name.
    Body: { "name": "2nd Rent" }
    """
    data = request.get_json()
    if not data or "name" not in data:
        return jsonify({"status": "error", "message": "Name is required"}), 400

    name = data["name"].strip()

    try:
        config_path = get_config_path()

        with open(config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        if "temporary_expenses" in config_data and config_data["temporary_expenses"]:
            original_len = len(config_data["temporary_expenses"])
            config_data["temporary_expenses"] = [
                e for e in config_data["temporary_expenses"] if e["name"] != name
            ]

            if len(config_data["temporary_expenses"]) < original_len:
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.dump(config_data, f, sort_keys=False)
                refresh_data()

        return jsonify({"status": "ok"})

    except Exception as e:
        logger.exception("Failed to delete temporary expense")
        return jsonify({"status": "error", "message": str(e)}), 500
