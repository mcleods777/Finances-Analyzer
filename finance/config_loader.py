from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime

import yaml


@dataclass
class ColumnMapping:
    date: str
    description: str
    amount: str | None = None
    debit: str | None = None
    credit: str | None = None


@dataclass
class AccountConfig:
    file: str
    name: str
    type: str  # checking, savings, credit_card, investment, loan
    columns: ColumnMapping
    date_format: str = "%m/%d/%Y"
    amount_sign: str = "standard"  # "standard" or "inverted"
    balance_column: str | None = None
    opening_balance: float | None = None
    opening_date: str | None = None


@dataclass
class PayPeriodConfig:
    start_date: date
    frequency_days: int = 14


@dataclass
class ClassificationConfig:
    income_keywords: list[str] = field(default_factory=list)
    expense_keywords: list[str] = field(default_factory=list)
    transfer_keywords: list[str] = field(default_factory=list)


@dataclass
class CategorizationRule:
    category: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class RecurringBill:
    name: str
    amount: float
    day_of_month: int
    match_criteria: list[str] = field(default_factory=list)


@dataclass
class TemporaryExpense:
    name: str
    amount: float
    half: int  # 1 = 1st-15th, 2 = 16th-end


@dataclass
class BudgetOverrides:
    first_half: float | None = None
    second_half: float | None = None


@dataclass
class AppConfig:
    pay_period: PayPeriodConfig
    accounts: list[AccountConfig]
    classification: ClassificationConfig
    categorization_rules: list[CategorizationRule] = field(default_factory=list)
    recurring_bills: list[RecurringBill] = field(default_factory=list)
    temporary_expenses: list[TemporaryExpense] = field(default_factory=list)
    budget_overrides: BudgetOverrides = field(default_factory=BudgetOverrides)


def _parse_column_mapping(raw: dict) -> ColumnMapping:
    return ColumnMapping(
        date=raw["date"],
        description=raw["description"],
        amount=raw.get("amount"),
        debit=raw.get("debit"),
        credit=raw.get("credit"),
    )


def _parse_account(raw: dict) -> AccountConfig:
    columns = _parse_column_mapping(raw["columns"])

    # Validate: must have either amount or both debit+credit
    if not columns.amount and not (columns.debit and columns.credit):
        raise ValueError(
            f"Account '{raw['name']}': must specify either 'amount' or both 'debit' and 'credit' columns"
        )

    return AccountConfig(
        file=raw["file"],
        name=raw["name"],
        type=raw["type"],
        columns=columns,
        date_format=raw.get("date_format", "%m/%d/%Y"),
        amount_sign=raw.get("amount_sign", "standard"),
        balance_column=raw.get("balance_column"),
        opening_balance=raw.get("opening_balance"),
        opening_date=raw.get("opening_date"),
    )


def _parse_pay_period(raw: dict) -> PayPeriodConfig:
    start = raw["start_date"]
    if isinstance(start, str):
        start = datetime.strptime(start, "%Y-%m-%d").date()
    elif isinstance(start, datetime):
        start = start.date()
    return PayPeriodConfig(
        start_date=start,
        frequency_days=raw.get("frequency_days", 14),
    )


def _parse_classification(raw: dict | None) -> ClassificationConfig:
    if raw is None:
        return ClassificationConfig()
    return ClassificationConfig(
        income_keywords=[kw.lower() for kw in raw.get("income_keywords", [])],
        expense_keywords=[kw.lower() for kw in raw.get("expense_keywords", [])],
        transfer_keywords=[kw.lower() for kw in raw.get("transfer_keywords", [])],
    )


def _parse_rules(raw: list[dict] | None) -> list[CategorizationRule]:
    if not raw:
        return []
    rules = []
    for r in raw:
        rules.append(CategorizationRule(
            category=r["category"],
            keywords=[k.lower() for k in r.get("keywords", [])]
        ))
    return rules


def _parse_recurring_bills(raw: list[dict] | None) -> list[RecurringBill]:
    if not raw:
        return []
    bills = []
    for b in raw:
        bills.append(RecurringBill(
            name=b["name"],
            amount=float(b["amount"]),
            day_of_month=int(b["day_of_month"]),
            match_criteria=[k.lower() for k in b.get("match_criteria", [])]
        ))
    return bills


def _parse_temporary_expenses(raw: list[dict] | None) -> list[TemporaryExpense]:
    if not raw:
        return []
    expenses = []
    for e in raw:
        expenses.append(TemporaryExpense(
            name=e["name"],
            amount=float(e["amount"]),
            half=int(e.get("half", 1)),
        ))
    return expenses


def _parse_budget_overrides(raw: dict | None) -> BudgetOverrides:
    if not raw:
        return BudgetOverrides()
    return BudgetOverrides(
        first_half=float(raw["first_half"]) if raw.get("first_half") is not None else None,
        second_half=float(raw["second_half"]) if raw.get("second_half") is not None else None,
    )


def load_config(config_path: str) -> AppConfig:
    """Load and parse config.yaml into typed dataclasses."""
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    pay_period = _parse_pay_period(raw["pay_period"])
    accounts = [_parse_account(a) for a in raw["accounts"]]
    classification = _parse_classification(raw.get("classification"))
    categorization_rules = _parse_rules(raw.get("categorization_rules"))
    recurring_bills = _parse_recurring_bills(raw.get("recurring_bills"))
    temporary_expenses = _parse_temporary_expenses(raw.get("temporary_expenses"))
    budget_overrides = _parse_budget_overrides(raw.get("budget_overrides"))

    return AppConfig(
        pay_period=pay_period,
        accounts=accounts,
        classification=classification,
        categorization_rules=categorization_rules,
        recurring_bills=recurring_bills,
        temporary_expenses=temporary_expenses,
        budget_overrides=budget_overrides,
    )


def validate_config(config: AppConfig, data_dir: str) -> list[str]:
    """Return a list of warnings/errors about the config."""
    issues = []

    valid_types = {"checking", "savings", "credit_card", "investment", "loan", "manual_balance"}
    for acct in config.accounts:
        filepath = os.path.join(data_dir, acct.file)
        if not os.path.exists(filepath):
            issues.append(f"CSV file not found: {filepath} (account: {acct.name})")
        if acct.type not in valid_types:
            issues.append(
                f"Unknown account type '{acct.type}' for '{acct.name}'. "
                f"Valid types: {', '.join(sorted(valid_types))}"
            )
        if acct.amount_sign not in ("standard", "inverted"):
            issues.append(
                f"Invalid amount_sign '{acct.amount_sign}' for '{acct.name}'. "
                f"Must be 'standard' or 'inverted'."
            )

    return issues
