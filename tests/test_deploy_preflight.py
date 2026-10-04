"""Task 4 (hybrid plan v4): deploy-time ingest preflight + ingest router
mounted into the dashboard app.

Two contracts under test:
* ``scripts/deploy/preflight_ingest.py`` — the FAIL-CLOSED deploy gate for
  ``STOCKGAP_INGEST_TOKEN`` (read from the ENVIRONMENT ONLY):
  - valid = present, >= 32 chars (same floor as stockgap.ingest.MIN_TOKEN_LEN),
    printable ASCII with NO whitespace/control chars -> exit 0, NO output;
  - anything else -> non-zero exit + exactly ONE clear stderr line naming the
    failing property ("not set" / "empty" / "too short" / "disallowed
    characters") — NEVER the value, never a prefix of it.
* ``stockgap.dashboard`` mounts ``stockgap.ingest.router`` unchanged: with the
  token unset the documented 503 "ingest disabled" fail-closed answers BOTH
  /ingest/rows and /ingest/state on the real dashboard app (auth first: no DB
  is touched), and with a valid env token the routes work against a tmp DB
  (mirroring tests/test_ingest.py conventions). Mounting must not disturb the
  existing dashboard surface (``/`` still renders, /ingest/* hidden from
  openapi).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
PREFLIGHT = REPO_ROOT / "scripts" / "deploy" / "preflight_ingest.py"

# Contract constants, restated independently of the implementation.
MIN_TOKEN_LEN = 32
TOKEN = "tok-" + "k" * (MIN_TOKEN_LEN - 4)  # exactly 32 chars (as test_ingest)
TS = "2026-10-04T00:00:00.000Z"


def run_preflight(token: str | None) -> subprocess.CompletedProcess:
    """Run the script with an OTHERWISE EMPTY environment (env-only contract)."""
    env: dict[str, str] = {}
    if token is not None:
        env["STOCKGAP_INGEST_TOKEN"] = token
    return subprocess.run(
        [sys.executable, str(PREFLIGHT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def assert_one_clear_line(proc, *, leaks: str | None = None, says: str = ""):
    """Non-zero, ONE stderr line naming the property, value NEVER echoed."""
    assert proc.returncode != 0, f"must fail closed, got rc=0 (stdout={proc.stdout!r})"
    assert proc.stdout == "", f"stdout must stay empty, got {proc.stdout!r}"
    lines = proc.stderr.strip().splitlines()
    assert len(lines) == 1, f"exactly ONE clear stderr line, got {proc.stderr!r}"
    assert lines[0].strip(), "the stderr line must not be blank"
    assert says in lines[0], f"message must state the failing property {says!r}: {lines[0]!r}"
    if leaks:
        assert leaks not in proc.stderr, "the token VALUE must never be printed"
        assert leaks not in proc.stdout
        prefix = leaks[:8]
        if len(prefix) >= 4:
            assert prefix not in proc.stderr, "not even a PREFIX of the value"
            assert prefix not in proc.stdout


# ------------------------- preflight: valid -> 0 ---------------------------
@pytest.mark.parametrize("token", [
    TOKEN,                              # exactly at the 32-char floor
    "b" * MIN_TOKEN_LEN,                # boundary, repeated char
    "z" * 64,                           # comfortably long
    "~!@#$%^&*()_+-=[]{}|;:',.<>?/" + "0123",  # punctuation-heavy printable
])
def test_valid_token_exits_zero_silently(token):
    proc = run_preflight(token)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""
    assert proc.stderr == ""


# --------------------------- preflight: fail closed ------------------------
def test_missing_token_fails_with_one_clear_line():
    assert_one_clear_line(run_preflight(None), says="not set")


@pytest.mark.parametrize("token,says", [
    ("", "empty"),                                      # set but empty
    ("s" * (MIN_TOKEN_LEN - 1), "too short"),           # 31 chars
    ("x", "too short"),                                 # single char
    ("SECRET-" + "z" * 24 + " ", "characters"),         # trailing SPACE (32 ch)
    ("SECRET-" + "z" * 23 + "\t!", "characters"),       # embedded TAB
    ("SECRET-" + "z" * 23 + "\x7f!", "characters"),     # DEL (control char)
    ("SECRET-" + "z" * 23 + "é!", "characters"),        # non-ASCII
    ("SECRET-" + "z" * 20 + "!", "too short"),          # short AND fine charset
])
def test_weak_token_fails_one_line_never_echoes_value(token, says):
    assert_one_clear_line(run_preflight(token), leaks=token or None, says=says)


def test_preflight_reads_env_only_no_argv_no_files(tmp_path):
    """A valid token on argv (not env) must NOT pass — env is the only source."""
    proc = subprocess.run(
        [sys.executable, str(PREFLIGHT), TOKEN],
        env={},  # empty env, token smuggled via argv only
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode != 0
    assert_one_clear_line(proc, says="not set")


# --------------------- dashboard mounts the ingest router ------------------
from stockgap import dashboard  # noqa: E402  (app under test)
from stockgap import ingest  # noqa: E402  (contract constants)


@pytest.fixture
def empty_env(monkeypatch):
    """EMPTY env at request time: no token, no DB override (auth runs first —
    the 503 path never touches the default DB)."""
    monkeypatch.delenv("STOCKGAP_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("STOCKGAP_DB", raising=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Mirror tests/test_ingest.py: tmp DB + valid 32-char token in env."""
    db = tmp_path / "spread_ticks.db"
    monkeypatch.setenv("STOCKGAP_DB", str(db))
    monkeypatch.setenv("STOCKGAP_INGEST_TOKEN", TOKEN)
    return db


@pytest.fixture
def client():
    return TestClient(dashboard.app)


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_ingest_router_is_mounted_and_hidden_on_dashboard(client):
    paths = {getattr(r, "path", None) for r in dashboard.app.routes}
    assert "/ingest/rows" in paths, "ingest router not mounted on dashboard.app"
    assert "/ingest/state" in paths
    schema = dashboard.app.openapi()
    assert "/ingest/rows" not in schema["paths"]
    assert "/ingest/state" not in schema["paths"]


def test_dashboard_boots_and_ingest_503_on_empty_env(client, empty_env):
    """Documented fail-closed: token missing/weak at REQUEST time -> 503
    'ingest disabled' on BOTH endpoints — and the app still serves pages."""
    assert client.get("/").status_code == 200  # dashboard boots with the mount
    r = client.post(
        "/ingest/rows",
        json={"table": "collector_meta", "mode": "snapshot", "rows": []},
        headers=auth(),
    )
    assert r.status_code == 503
    assert r.text == "ingest disabled"
    r = client.get("/ingest/state", headers=auth())
    assert r.status_code == 503
    assert r.text == "ingest disabled"


def test_ingest_short_token_503_on_dashboard_app(client, empty_env, monkeypatch):
    monkeypatch.setenv("STOCKGAP_INGEST_TOKEN", TOKEN[:MIN_TOKEN_LEN - 1])
    r = client.post(
        "/ingest/rows",
        json={"table": "collector_meta", "mode": "snapshot", "rows": []},
        headers=auth(),
    )
    assert r.status_code == 503
    assert r.text == "ingest disabled"


def test_ingest_routes_work_on_dashboard_with_valid_token(client, env):
    """Mounted routes really write against the tmp DB (test_ingest conventions)."""
    rows = [{"key": "last_tick_ts", "value": TS}, {"key": "universe_count", "value": 3}]
    r = client.post(
        "/ingest/rows",
        json={"table": "collector_meta", "mode": "snapshot", "rows": rows},
        headers=auth(),
    )
    assert r.status_code == 200
    assert r.json() == {"table": "collector_meta", "rows": 2}

    r = client.get("/ingest/state", headers=auth())
    assert r.status_code == 200
    assert set(r.json()["tables"]) == set(ingest.INGEST_TABLES)

    # wrong token is still 401 through the dashboard mount
    assert client.get("/ingest/state", headers=auth("w" * MIN_TOKEN_LEN)).status_code == 401


def test_mount_does_not_disturb_existing_dashboard_surface(client, empty_env):
    """The include_router diff must not change the pre-existing app surface."""
    assert client.get("/").status_code == 200
    paths = {getattr(r, "path", None) for r in dashboard.app.routes}
    assert {"/", "/frag/gaps", "/frag/cross", "/frag/oraculo", "/frag/sistema"} <= paths


# -------- B2/N7: an ingest import/mount failure must NEVER kill the boot ----

class _BoomRouter:
    """Any attribute access raises — a stand-in for a broken ingest router."""

    def __getattr__(self, name):
        raise RuntimeError("simulated ingest mount failure")


@pytest.mark.parametrize("failure", ["import", "mount"])
def test_dashboard_boots_and_serves_when_ingest_import_or_mount_raises(
        failure, caplog):
    """N7 regression pin (plan v4 Task 4): the guarded ingest import+mount
    must NEVER crash the live dashboard. When it fails the app still boots,
    non-ingest routes serve, /ingest/* is simply absent, and the failure is
    logged LOUDLY ('ingest router UNAVAILABLE: <err>') — never swallowed
    silently. After the test the fully-mounted app is restored."""
    import builtins
    import importlib

    real_import = builtins.__import__
    real_router = ingest.router
    expected_err = f"simulated ingest {failure} failure"

    def boom_import(name, globals=None, locals=None, fromlist=(), level=0):
        # `from . import ingest` compiles to __import__('', fromlist=('ingest',), 1)
        if name == "stockgap.ingest" or (level == 1 and "ingest" in (fromlist or ())):
            raise ImportError(expected_err)
        return real_import(name, globals, locals, fromlist, level)

    try:
        if failure == "import":
            builtins.__import__ = boom_import
        else:
            ingest.router = _BoomRouter()
        importlib.reload(dashboard)  # red phase: this raises — boot crash
        paths = {getattr(r, "path", None) for r in dashboard.app.routes}
        assert "/ingest/rows" not in paths, "/ingest/* must be absent"
        assert "/ingest/state" not in paths, "/ingest/* must be absent"
        c = TestClient(dashboard.app)
        assert c.get("/").status_code == 200          # non-ingest route serves
        assert c.get("/ingest/rows").status_code == 404
        assert c.get("/ingest/state").status_code == 404
        assert "ingest router UNAVAILABLE" in caplog.text  # loud log line
        assert expected_err in caplog.text            # failure NAMED, not swallowed
    finally:
        builtins.__import__ = real_import
        ingest.router = real_router
        importlib.reload(dashboard)  # restore the fully-mounted app

    # happy path intact for the rest of the suite
    paths = {getattr(r, "path", None) for r in dashboard.app.routes}
    assert "/ingest/rows" in paths and "/ingest/state" in paths
