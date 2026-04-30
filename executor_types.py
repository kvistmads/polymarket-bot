"""
executor_types.py — Delte dataklasser for executor-moduler.

Importeres af: executor.py, executor_gates.py, executor_clob.py
Holdes separat for at undgå cirkulære imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class TradeEvent:
    """Repræsenterer én trade_event-række fra databasen."""

    id: int
    wallet_id: int
    wallet_address: str
    wallet_label: str | None
    condition_id: str
    outcome: str
    event_type: str  # 'opened' | 'closed' | 'resized'
    new_size: Decimal
    price_at_event: Decimal | None


@dataclass
class OrderResult:
    """Resultat af én ordre — paper eller live."""

    status: str  # 'filled' | 'failed' | 'paper' | 'cancelled'
    size_filled: Decimal | None
    price: Decimal | None
    error_msg: str | None
