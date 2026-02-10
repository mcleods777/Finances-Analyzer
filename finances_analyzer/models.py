from dataclasses import dataclass
from datetime import date


@dataclass
class Transaction:
    date: date
    category: str
    amount: float
    description: str
