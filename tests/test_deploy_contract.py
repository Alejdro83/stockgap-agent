"""Deploy contract for scripts/deploy/entrypoint.sh — the STOCKGAP_MODE guard
(Task 2 of the hybrid collector sync plan, R4/R12).

Domain facts under test:
* ``STOCKGAP_MODE=dashboard`` runs a dashboard-only container: the fetch loops
  (b) collector loop/watchdog (``bash scripts/ensure_collector.sh``), (c1)
  ``flock -n data/oracle.lock python3 stockgap/oracle.py --once`` and (c2)
  ``python3 scripts/alerts.py`` must ALL sit behind
  ``[ "${STOCKGAP_MODE:-full}" = "full" ]`` — a dashboard-only container must
  never double-fire alerts or create a second DB writer (hybrid: data arrives
  via POST /ingest).
* (e) rotation (``python3 scripts/rotate_data.py``) and (d) the collector-log
  ``tail -F`` stay OUTSIDE the guard — rotation is useful in both modes.
* The default (STOCKGAP_MODE unset) path is byte-identical to pre-guard
  behavior: the guard defaults to ``full``.
* The final startup log line reflects the mode; dashboard mode logs
  ``MODE=dashboard: fetch loops disabled (hybrid: data arrives via /ingest)``
  and must NOT claim ``collector + watchdog`` was started.

Static tests parse the script text (comment lines excluded). The behavioral
tests stub ``python3``/``bash`` on PATH and run the entrypoint for ~3 s per
mode under ``timeout``; because DATA=/data is a Northflank volume path that
does not exist on the host, the run uses a tmp copy with only the ``REPO=`` /
``DATA=`` constants redirected to tmp dirs (asserted) — same code, no touchy
of /data or the live repo.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = (
    Path(__file__).resolve().parent.parent / "scripts" / "deploy" / "entrypoint.sh"
)

# The guard condition, literal (R4). The ``:-full`` default is the compat pin:
# STOCKGAP_MODE unset -> today's behavior exactly.
GUARD = '[ "${STOCKGAP_MODE:-full}" = "full" ]'

# The three fetch-loop commands, named explicitly (R12/R4).
LOOP_NEEDLES = (
    "bash scripts/ensure_collector.sh",       # (b) collector + watchdog
    "python3 stockgap/oracle.py --once",      # (c1) oracle loop
    "python3 scripts/alerts.py",              # (c2) alerts loop
)
ROTATION_NEEDLE = "python3 scripts/rotate_data.py"   # (e) — kept OUTSIDE
TAIL_NEEDLE = "tail -n 0 -F"                         # (d) — kept OUTSIDE

DASHBOARD_LOG = "MODE=dashboard: fetch loops disabled (hybrid: data arrives via /ingest)"
FULL_START_LOG = (
    "all processes started (dashboard + collector + watchdog + oracle + alerts"
    " + collector-log tail)"
)


def _code_lines(text: str) -> list:
    """Non-empty, non-comment-only lines (comments may mention the commands)."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(line)
    return out


def _guard_blocks(lines):
    """[(then_lines, else_lines)] for every ``if GUARD`` block (depth-matched)."""
    blocks = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith("if ") or GUARD not in s:
            continue
        depth = 1
        then_lines, else_lines = [], []
        in_else = False
        for l in lines[i + 1:]:
            t = l.strip()
            if re.match(r"if[\s!]", t) or t.startswith("if "):
                depth += 1
            if re.match(r"^fi\b", t):
                depth -= 1
                if depth == 0:
                    break
            if depth == 1 and re.match(r"^else\b", t):
                in_else = True
                continue
            (else_lines if in_else else then_lines).append(l)
        blocks.append((then_lines, else_lines))
    return blocks


@pytest.fixture(scope="module")
def entrypoint_text() -> str:
    return ENTRYPOINT.read_text()


@pytest.fixture(scope="module")
def code_lines(entrypoint_text) -> list:
    return _code_lines(entrypoint_text)


@pytest.fixture(scope="module")
def blocks(code_lines) -> list:
    return _guard_blocks(code_lines)


def _where(code_lines, blocks, needle):
    """All code lines containing needle, tagged ('then'|'else'|'outside', text)."""
    # Rebuild lookup by identity over the same list objects the parser kept.
    tag = {}
    for then_lines, else_lines in blocks:
        for l in then_lines:
            tag[id(l)] = "then"
        for l in else_lines:
            tag[id(l)] = "else"
    hits = []
    for l in code_lines:
        if needle in l:
            hits.append((tag.get(id(l), "outside"), l.strip()))
    return hits


def test_entrypoint_exists():
    assert ENTRYPOINT.is_file()


def test_guard_present_and_defaults_to_full(entrypoint_text, blocks):
    """Compat pin: `[ "${STOCKGAP_MODE:-full}" = "full" ]` — unset means full."""
    assert entrypoint_text.count(GUARD) >= 1
    assert len(blocks) >= 1


def test_all_three_loops_are_inside_the_guard(code_lines, blocks):
    assert blocks, "STOCKGAP_MODE guard missing"
    for needle in LOOP_NEEDLES:
        hits = _where(code_lines, blocks, needle)
        assert hits, f"loop command not found in entrypoint: {needle!r}"
        outside = [l for where, l in hits if where == "outside"]
        assert not outside, (
            f"{needle!r} runs OUTSIDE the STOCKGAP_MODE guard (would double-fire "
            f"in dashboard mode): {outside}"
        )


def test_alerts_loop_is_guarded_and_named_explicitly(code_lines, blocks):
    """(c2) the alerts loop — command named explicitly (R4)."""
    assert blocks, "STOCKGAP_MODE guard missing"
    hits = _where(code_lines, blocks, "python3 scripts/alerts.py")
    assert hits
    assert all(where != "outside" for where, _ in hits)


def test_rotation_stays_outside_the_guard(code_lines, blocks):
    """(e) rotation runs in BOTH modes — never guarded (R4)."""
    assert blocks, "STOCKGAP_MODE guard missing"
    hits = _where(code_lines, blocks, ROTATION_NEEDLE)
    assert hits, "rotation loop command missing from entrypoint"
    inside = [l for where, l in hits if where != "outside"]
    assert not inside, f"rotation must stay OUTSIDE the guard, found inside: {inside}"


def test_collector_log_tail_stays_outside_the_guard(code_lines, blocks):
    """(d) tail -F runs in BOTH modes — never guarded."""
    assert blocks, "STOCKGAP_MODE guard missing"
    hits = _where(code_lines, blocks, TAIL_NEEDLE)
    assert hits, "collector-log tail missing from entrypoint"
    inside = [l for where, l in hits if where != "outside"]
    assert not inside, f"tail -F must stay OUTSIDE the guard, found inside: {inside}"


def test_default_startup_log_line_unchanged(blocks):
    """Full mode keeps the pre-guard final log line verbatim, in the then-branch."""
    assert blocks, "STOCKGAP_MODE guard missing"
    then_all = "\n".join(l for then_lines, _ in blocks for l in then_lines)
    assert FULL_START_LOG in then_all


def test_dashboard_mode_logs_the_mode_and_never_claims_collector(blocks):
    """Dashboard logs the exact mode line (in the else-branch) and must NOT
    claim 'collector + watchdog' started (R4)."""
    assert blocks, "STOCKGAP_MODE guard missing"
    else_all = "\n".join(l for _, else_lines in blocks for l in else_lines)
    assert DASHBOARD_LOG in else_all
    assert "collector + watchdog" not in else_all


# ---------------------------------------------------------------------------
# Behavioral variant: stub python3/bash on PATH, run the entrypoint ~3 s per
# mode under timeout, and assert the loop commands never EXECUTE in dashboard
# mode while rotation does. DATA=/data does not exist on the host, so the run
# uses a tmp copy with only REPO=/DATA= redirected (asserted) — same code.
# ---------------------------------------------------------------------------

STUB_PY3 = """#!/bin/sh
echo "python3 $*" >> "$STUB_CALLS"
exit 0
"""
STUB_BASH = """#!/bin/sh
echo "bash $*" >> "$STUB_CALLS"
exit 0
"""


def run_entrypoint(tmp_path: Path, mode):
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    data = tmp_path / "volume"
    data.mkdir()

    text = ENTRYPOINT.read_text()
    n_re = len(re.findall(r'(?m)^REPO="', text))
    n_da = len(re.findall(r'(?m)^DATA="', text))
    assert n_re == 1 and n_da == 1, (
        f"unexpected entrypoint constants (REPO x{n_re}, DATA x{n_da}) — "
        "update this test's redirection"
    )
    patched = re.sub(r'(?m)^REPO="[^"]*"', f'REPO="{repo}"', text)
    patched = re.sub(r'(?m)^DATA="[^"]*"', f'DATA="{data}"', patched)
    script = tmp_path / "entrypoint.sh"
    script.write_text(patched)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for name, body in (("python3", STUB_PY3), ("bash", STUB_BASH)):
        p = stub_bin / name
        p.write_text(body)
        p.chmod(0o755)

    calls = tmp_path / "calls.log"
    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}:{env['PATH']}"
    env["STUB_CALLS"] = str(calls)
    if mode is None:
        env.pop("STOCKGAP_MODE", None)   # default path: guard must default to full
    else:
        env["STOCKGAP_MODE"] = mode

    proc = subprocess.run(
        ["/usr/bin/timeout", "-k", "15", "3", "/bin/bash", str(script)],
        env=env,
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert proc.returncode in (0, 124), (
        f"entrypoint rc={proc.returncode}\nstdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    return calls.read_text().splitlines() if calls.exists() else []


def test_behavior_default_mode_executes_all_loops_and_rotation(tmp_path):
    """STOCKGAP_MODE unset == today's behavior: every loop fires, plus rotation."""
    calls = "\n".join(run_entrypoint(tmp_path, None))
    assert "bash scripts/ensure_collector.sh" in calls
    assert "python3 stockgap/oracle.py --once" in calls
    assert "python3 scripts/alerts.py" in calls
    assert "python3 scripts/rotate_data.py" in calls
    assert "python3 scripts/dashboard.py" in calls


def test_behavior_explicit_full_mode_executes_all_loops(tmp_path):
    calls = "\n".join(run_entrypoint(tmp_path, "full"))
    assert "python3 stockgap/oracle.py --once" in calls
    assert "python3 scripts/alerts.py" in calls
    assert "bash scripts/ensure_collector.sh" in calls
    assert "python3 scripts/rotate_data.py" in calls


def test_behavior_dashboard_mode_never_executes_loops_but_rotation_runs(tmp_path):
    """The core R4 claim: dashboard-only container cannot double-fire alerts
    or become a second writer — loop commands never execute, rotation does."""
    calls = "\n".join(run_entrypoint(tmp_path, "dashboard"))
    assert "bash scripts/ensure_collector.sh" not in calls
    assert "python3 stockgap/oracle.py --once" not in calls
    assert "python3 scripts/alerts.py" not in calls
    assert "python3 scripts/rotate_data.py" in calls, "rotation must run in dashboard mode"
    assert "python3 scripts/dashboard.py" in calls, "the dashboard itself must run"
