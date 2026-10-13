"""Matching tolerances and severity thresholds."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field

_ENV_PREFIX = "OPSRESOLVE_"


class MatchConfig(BaseModel):
    """Deterministic business-rule parameters.

    Defaults are strict (zero tolerance). Tolerances are inclusive: a
    difference exactly equal to the tolerance is *not* an exception.
    """

    model_config = ConfigDict(frozen=True)

    price_tolerance_pct: Decimal = Field(default=Decimal("0"), ge=0)
    price_tolerance_abs: Decimal = Field(default=Decimal("0.00"), ge=0)
    quantity_tolerance: Decimal = Field(default=Decimal("0"), ge=0)
    duplicate_window_days: int = Field(default=30, ge=0)
    high_severity_amount: Decimal = Field(default=Decimal("1000.00"), ge=0)
    medium_severity_amount: Decimal = Field(default=Decimal("100.00"), ge=0)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "MatchConfig":
        environ = os.environ if environ is None else environ
        overrides = {}
        for name in cls.model_fields:
            raw = environ.get(_ENV_PREFIX + name.upper())
            if raw is not None and raw.strip() != "":
                overrides[name] = raw.strip()
        return cls(**overrides)
