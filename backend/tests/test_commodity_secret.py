"""Commodity-only file-backed market credential; sentinel values only."""
from types import SimpleNamespace

import pytest

from app.commodity_acts.broker import CommodityDataError, MarketOnlySDK, make_adapter
from tests.test_scalper import settings


def test_private_file_overrides_placeholder_in_memory_only(tmp_path, monkeypatch):
    file = tmp_path / "key"
    file.write_text("SYNTHETIC_SENTINEL_ONLY\n")
    file.chmod(0o400)
    seen = []

    def create(cfg):
        seen.append(cfg.kotak_consumer_key.get_secret_value())
        return SimpleNamespace(search_scrip=lambda **kw: [], quotes=lambda **kw: [],
                               place_order=lambda **kw: pytest.fail("order path forbidden"))

    monkeypatch.setattr("app.broker.kotak.auth.create_market_data_client", create)
    original = settings()
    previous = original.kotak_consumer_key.get_secret_value()
    adapter = make_adapter(original, key_file=str(file))
    assert seen == ["SYNTHETIC_SENTINEL_ONLY"]
    assert original.kotak_consumer_key.get_secret_value() == previous
    assert isinstance(adapter._client, MarketOnlySDK)
    assert not hasattr(adapter._client, "place_order")


@pytest.mark.parametrize("failure", ["missing", "public", "symlink", "empty", "oversized", "placeholder", "whitespace"])
def test_unsafe_secret_is_refused_without_client_or_secret_disclosure(tmp_path, monkeypatch, failure):
    file = tmp_path / "key"
    sentinel = "SYNTHETIC_PRIVATE_VALUE"
    file.write_text(sentinel)
    file.chmod(0o400)
    if failure == "missing":
        file.unlink()
    elif failure == "public":
        file.chmod(0o644)
    elif failure == "symlink":
        link = tmp_path / "link"
        link.symlink_to(file)
        file = link
    else:
        file.chmod(0o600)
        file.write_text({"empty": "", "oversized": "X" * 16385,
                         "placeholder": "COMMODITY_DISABLED_NO_CREDENTIAL", "whitespace": "BAD KEY"}[failure])
        file.chmod(0o400)
    monkeypatch.setattr("app.broker.kotak.auth.create_market_data_client", lambda cfg: pytest.fail("client must not be created"))
    with pytest.raises(CommodityDataError) as error:
        make_adapter(settings(), key_file=str(file))
    assert str(error.value) == "COMMODITY_MARKET_DATA_SECRET_FILE_REFUSED"
    assert sentinel not in str(error.value)
