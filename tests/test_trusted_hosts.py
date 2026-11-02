from bankiru.config import get_settings


def test_trusted_hosts_parses_gateway(monkeypatch) -> None:
    monkeypatch.setenv("TRUSTED_HOSTS", "172.16.89.1")
    get_settings.cache_clear()
    try:
        assert get_settings().TRUSTED_HOSTS == ["172.16.89.1"]
    finally:
        get_settings.cache_clear()
