"""SENTRY_DSN toggle: no-DSN no-op, init when DSN set (mocked), app still boots."""
import logging
from unittest.mock import Mock

from fastapi.testclient import TestClient

from stockgap import telemetry


def _reset(monkeypatch, tmp_path):
    """Fresh toggle state per test: no DSN anywhere, no config/sentry.env file."""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(telemetry, "CONFIG_FILE", tmp_path / "sentry.env")  # absent
    monkeypatch.setattr(telemetry, "_init_state", None)


def test_no_dsn_is_noop(monkeypatch, tmp_path, caplog):
    import sentry_sdk

    _reset(monkeypatch, tmp_path)
    fake_init = Mock()
    monkeypatch.setattr(sentry_sdk, "init", fake_init)
    with caplog.at_level(logging.INFO, logger="stockgap.telemetry"):
        assert telemetry.init_sentry("stockgap-test") is False
    fake_init.assert_not_called()  # sentry_sdk is never touched without a DSN
    assert any("no SENTRY_DSN" in r.getMessage() for r in caplog.records)


def test_init_called_when_dsn_set(monkeypatch, tmp_path):
    import sentry_sdk

    _reset(monkeypatch, tmp_path)
    fake_init, fake_tag = Mock(), Mock()
    monkeypatch.setattr(sentry_sdk, "init", fake_init)
    monkeypatch.setattr(sentry_sdk, "set_tag", fake_tag)
    monkeypatch.setenv("SENTRY_DSN", "https://key@example.com/42")
    assert telemetry.init_sentry("stockgap-test", fastapi=True) is True
    fake_init.assert_called_once()
    assert fake_init.call_args.kwargs["dsn"] == "https://key@example.com/42"
    assert fake_init.call_args.kwargs["integrations"] != []  # FastAPI integration
    fake_tag.assert_called_once_with("service", "stockgap-test")


def test_dashboard_app_boots(monkeypatch, tmp_path):
    """Import path with the toggle wired must still serve the dashboard shell."""
    _reset(monkeypatch, tmp_path)  # no SENTRY_DSN: the default deployed posture
    from stockgap import dashboard

    resp = TestClient(dashboard.app).get("/")
    assert resp.status_code == 200
