import numpy as np
import pandas as pd
import pytest

from core.config import load_settings


@pytest.fixture
def cfg(tmp_path):
    s = load_settings()
    s.raw["paths"]["cache_dir"] = str(tmp_path / "cache")
    return s


def make_ohlcv(closes, volume=1_000_000, spread=0.01, start="2025-01-01"):
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(start, periods=len(closes))
    vol = np.full(len(closes), volume) if np.isscalar(volume) else np.asarray(volume)
    return pd.DataFrame(
        {"open": closes, "high": closes * (1 + spread), "low": closes * (1 - spread),
         "close": closes, "volume": vol},
        index=idx,
    )
