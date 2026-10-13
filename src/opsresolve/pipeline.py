"""Orchestrates ingestion -> matching -> detection. No I/O formatting here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from opsresolve.config import MatchConfig
from opsresolve.detection import RejectedKeys, detect_exceptions
from opsresolve.ingestion import Dataset, load_dataset
from opsresolve.matching import MatchResult, match_records
from opsresolve.models import OpsException


@dataclass
class InvestigationResult:
    dataset: Dataset
    match: MatchResult
    exceptions: list[OpsException]
    config: MatchConfig
    generated_at: datetime


def investigate(dataset: Dataset, config: MatchConfig | None = None) -> InvestigationResult:
    config = config or MatchConfig()
    match = match_records(
        dataset.purchase_orders.records, dataset.invoices.records, dataset.goods_received.records)
    rejected = RejectedKeys(
        purchase_orders=dataset.purchase_orders.rejected_keys,
        invoices=dataset.invoices.rejected_keys,
        goods_received=dataset.goods_received.rejected_keys,
    )
    exceptions = detect_exceptions(match, dataset.invoices.records, config, rejected)
    return InvestigationResult(dataset, match, exceptions, config, datetime.now(timezone.utc))


def investigate_files(
    po_path: str | Path, invoice_path: str | Path, grn_path: str | Path, config: MatchConfig | None = None
) -> InvestigationResult:
    return investigate(load_dataset(po_path, invoice_path, grn_path), config)
