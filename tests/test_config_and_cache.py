import pytest

from core.config import DEFAULT_SETTINGS, load_settings
from core.providers.base import DiskCache


def test_settings_load_and_dotted_access():
    s = load_settings(DEFAULT_SETTINGS)
    assert s.get("filters.min_price") == 3.0
    assert s.get("filters.min_avg_dollar_volume") == 10_000_000
    assert s.get("nope.missing", None) is None
    with pytest.raises(KeyError):
        s.get("nope.missing")


def test_env_resolution(monkeypatch):
    s = load_settings(DEFAULT_SETTINGS)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    assert s.env("providers.edgar.user_agent_env") is None
    monkeypatch.setenv("SEC_USER_AGENT", "stock-picker test@example.com")
    assert s.env("providers.edgar.user_agent_env") == "stock-picker test@example.com"


def test_no_secrets_in_settings_file():
    """Any key/secret/token/agent setting must be an `*_env` field naming an env var."""
    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from walk(v, f"{path}.{k}" if path else k)
        else:
            yield path, node

    sensitive = ("key", "secret", "token", "password", "user_agent")
    for path, value in walk(load_settings(DEFAULT_SETTINGS).raw):
        leaf = path.rsplit(".", 1)[-1].lower()
        if any(word in leaf for word in sensitive):
            assert leaf.endswith("_env"), f"{path} should name an env var (use {leaf}_env)"
            assert isinstance(value, str) and value.isupper(), f"{path}={value!r} doesn't look like an env var name"


def test_disk_cache_ttl(tmp_path):
    c = DiskCache(tmp_path, "t")
    calls = []

    def fetch():
        calls.append(1)
        return 42

    assert c.get_or_fetch("k", 1, fetch).value == 42
    assert c.get_or_fetch("k", 1, fetch).value == 42
    assert len(calls) == 1
    c.get_or_fetch("k", 0, fetch)                # expired -> refetch
    assert len(calls) == 2


def test_disk_cache_atomic_and_stale_ok(tmp_path, monkeypatch):
    from core.providers.base import STALE_OK_ENV
    c = DiskCache(tmp_path, "t")
    c.put("k", 1)
    assert not list((tmp_path / "t").glob("*.tmp"))          # no temp files left behind
    assert c.get("k", 0) is None                             # expired normally
    monkeypatch.setenv(STALE_OK_ENV, "1")
    assert c.get("k", 0).value == 1                          # quick mode accepts the cached copy
