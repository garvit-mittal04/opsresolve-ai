from decimal import Decimal

import pytest
from pydantic import ValidationError

from opsresolve.config import MatchConfig


def test_defaults_are_strict():
    cfg = MatchConfig()
    assert cfg.price_tolerance_pct == 0 and cfg.price_tolerance_abs == 0 and cfg.quantity_tolerance == 0


def test_from_env_overrides_and_ignores_blank():
    cfg = MatchConfig.from_env({"OPSRESOLVE_PRICE_TOLERANCE_PCT": "2.5", "OPSRESOLVE_DUPLICATE_WINDOW_DAYS": " ",
                                "UNRELATED": "x"})
    assert cfg.price_tolerance_pct == Decimal("2.5")
    assert cfg.duplicate_window_days == 30


def test_from_env_rejects_invalid_values():
    with pytest.raises(ValidationError):
        MatchConfig.from_env({"OPSRESOLVE_QUANTITY_TOLERANCE": "abc"})
