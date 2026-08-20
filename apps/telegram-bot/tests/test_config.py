from environs import Env
import pytest

from config import Webhook


def test_polling_does_not_require_webhook_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_WEBHOOK", "false")
    monkeypatch.delenv("SERVER_URL", raising=False)
    monkeypatch.delenv("DOMAIN", raising=False)

    config = Webhook.from_env(Env())

    assert config.use_webhook is False
    assert config.url == ""
    assert config.domain == ""


def test_webhook_requires_url_and_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_WEBHOOK", "true")
    monkeypatch.delenv("SERVER_URL", raising=False)
    monkeypatch.delenv("DOMAIN", raising=False)

    with pytest.raises(ValueError, match="SERVER_URL and DOMAIN"):
        Webhook.from_env(Env())
