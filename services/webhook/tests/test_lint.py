"""Tests for the ruff lint-evidence source (#681, epic #707).

The source is OFF by default (GRUG_LINT_EVIDENCE), so every test that expects
findings enables it explicitly - and one test pins the default-off contract.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import yaml

from personas.code_reviewer.diff_parser import DiffHunk
import personas.code_reviewer.lint as lint_mod
from personas.code_reviewer.lint import scan_ruff


import pytest


@pytest.fixture(autouse=True)
def _enable_lint(monkeypatch):
    """Every test below exercises the scanner, so turn it on. The default-off
    contract is pinned separately in test_disabled_by_default."""
    monkeypatch.setattr(lint_mod, "_ENABLED", True)


def _hunk(path: str, body: str, new_start: int = 1) -> DiffHunk:
    return DiffHunk(
        file_path=path, new_start=new_start,
        new_lines=frozenset(), body=body,
    )


# a two-added-line hunk: lines 1 and 2 of the new side
_HUNK_BODY = "@@ -0,0 +1,2 @@\n+import os\n+x = 1\n"

_RUFF_JSON = json.dumps([
    {
        "filename": "/tmp/grug-lint-abc/services/x.py",
        "code": "F401",
        "message": "`os` imported but unused",
        "location": {"row": 1, "column": 8},
    },
])


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ruff"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def test_exit_1_is_violations_not_failure():
    """THE trap this module exists to avoid: ruff exits 1 when it FINDS
    violations. Treating non-zero as failure (as scan_semgrep correctly does
    for semgrep) would discard every real result and degrade to a permanent
    silent zero."""
    with patch("subprocess.run", return_value=_proc(1, _RUFF_JSON)), \
         patch("tempfile.TemporaryDirectory") as td:
        td.return_value.__enter__.return_value = "/tmp/grug-lint-abc"
        found = scan_ruff((_hunk("services/x.py", _HUNK_BODY),), {"services/x.py": "import os\nx = 1\n"})
    assert len(found) == 1
    assert found[0].file == "services/x.py"
    assert found[0].line == 1
    assert "F401" in found[0].message
    assert found[0].severity == "medium"  # advisory, never blocks alone


def test_exit_2_is_a_real_error_and_degrades():
    with patch("subprocess.run", return_value=_proc(2, "", "ruff: bad config")), \
         patch("tempfile.TemporaryDirectory") as td:
        td.return_value.__enter__.return_value = "/tmp/grug-lint-abc"
        assert scan_ruff((_hunk("services/x.py", _HUNK_BODY),), {"services/x.py": "x = 1\n"}) == ()


def test_violation_on_untouched_line_is_dropped():
    """Diff-anchored: a pre-existing violation the PR did not touch is not
    this PR's problem."""
    payload = json.dumps([{
        "filename": "/tmp/grug-lint-abc/services/x.py",
        "code": "E501", "message": "line too long",
        "location": {"row": 99, "column": 1},
    }])
    with patch("subprocess.run", return_value=_proc(1, payload)), \
         patch("tempfile.TemporaryDirectory") as td:
        td.return_value.__enter__.return_value = "/tmp/grug-lint-abc"
        assert scan_ruff((_hunk("services/x.py", _HUNK_BODY),), {"services/x.py": "x = 1\n"}) == ()


def test_missing_binary_degrades_silently():
    with patch("subprocess.run", side_effect=FileNotFoundError), \
         patch("tempfile.TemporaryDirectory") as td:
        td.return_value.__enter__.return_value = "/tmp/grug-lint-abc"
        assert scan_ruff((_hunk("services/x.py", _HUNK_BODY),), {"services/x.py": "x = 1\n"}) == ()


def test_timeout_degrades_silently():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ruff", 30)), \
         patch("tempfile.TemporaryDirectory") as td:
        td.return_value.__enter__.return_value = "/tmp/grug-lint-abc"
        assert scan_ruff((_hunk("services/x.py", _HUNK_BODY),), {"services/x.py": "x = 1\n"}) == ()


def test_unparseable_output_degrades_silently():
    with patch("subprocess.run", return_value=_proc(1, "not json")), \
         patch("tempfile.TemporaryDirectory") as td:
        td.return_value.__enter__.return_value = "/tmp/grug-lint-abc"
        assert scan_ruff((_hunk("services/x.py", _HUNK_BODY),), {"services/x.py": "x = 1\n"}) == ()


def test_malformed_result_row_is_skipped_not_fatal():
    payload = json.dumps([
        {"filename": "/tmp/grug-lint-abc/services/x.py", "code": "F401"},  # no location
        {"filename": "/tmp/grug-lint-abc/services/x.py", "code": "F401",
         "message": "unused", "location": {"row": 1, "column": 1}},
    ])
    with patch("subprocess.run", return_value=_proc(1, payload)), \
         patch("tempfile.TemporaryDirectory") as td:
        td.return_value.__enter__.return_value = "/tmp/grug-lint-abc"
        found = scan_ruff((_hunk("services/x.py", _HUNK_BODY),), {"services/x.py": "x = 1\n"})
    assert len(found) == 1


def test_non_python_files_are_not_scanned():
    assert scan_ruff((_hunk("web/app.ts", _HUNK_BODY),), {"web/app.ts": "let x = 1\n"}) == ()


def test_no_file_contents_returns_empty():
    assert scan_ruff((_hunk("services/x.py", _HUNK_BODY),), {}) == ()


def test_path_escape_is_refused():
    assert scan_ruff((_hunk("../etc/x.py", _HUNK_BODY),), {"../etc/x.py": "x = 1\n"}) == ()


def test_no_cache_flag_is_passed():
    """readOnlyRootFilesystem + uid 10001 --no-create-home: a cache write is
    what crashed semgrep live on 2026-07-13. Disable it outright."""
    with patch("subprocess.run", return_value=_proc(0, "[]")) as run, \
         patch("tempfile.TemporaryDirectory") as td:
        td.return_value.__enter__.return_value = "/tmp/grug-lint-abc"
        scan_ruff((_hunk("services/x.py", _HUNK_BODY),), {"services/x.py": "x = 1\n"})
    assert "--no-cache" in run.call_args[0][0]


def test_disabled_by_default(monkeypatch):
    """The gate is the safety property: a new finding source must not start
    firing on every PR in the fleet the moment it merges."""
    monkeypatch.setattr(lint_mod, "_ENABLED", False)
    with patch("subprocess.run", return_value=_proc(1, _RUFF_JSON)) as run:
        assert scan_ruff((_hunk("services/x.py", _HUNK_BODY),), {"services/x.py": "import os\n"}) == ()
    run.assert_not_called()  # not even spawned


# --- rule selection: the #707 recall fix ------------------------------------

def test_security_and_bug_families_are_selected():
    """The whole point of the change. Ruff's DEFAULTS (E4,E7,E9,F) carry ZERO
    security rules; without an explicit --select this module could not
    contribute to the recall gap it exists to close."""
    assert "S" in lint_mod._SELECT.split(",")       # flake8-bandit, security
    assert "B" in lint_mod._SELECT.split(",")       # flake8-bugbear, real bugs
    assert "ASYNC" in lint_mod._SELECT.split(",")


def test_measured_noise_rules_are_ignored():
    """Each exclusion was chosen from a measurement over grug's own tree
    (4425 -> 125), not a hunch. S101 alone was 97% of it."""
    ignored = lint_mod._IGNORE.split(",")
    assert "S101" in ignored    # assert-used: 4269 of 4284 hits were in tests
    assert "B008" in ignored    # fires on FastAPI's required Depends() default
    assert "S603" in ignored and "S607" in ignored  # subprocess trust undecidable


def test_ruff_is_invoked_with_the_selection(monkeypatch):
    """A --select that never reaches the command line is the failure mode
    that would silently restore the old zero-security behaviour."""
    seen = {}

    class _Proc:
        returncode = 0
        stdout = "[]"
        stderr = ""

    def _run(cmd, **kw):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(lint_mod.subprocess, "run", _run)  # _enable_lint fixture already turns it on
    scan_ruff((_hunk("a.py", "@@ -0,0 +1 @@\n+x = 1\n"),), {"a.py": "x = 1\n"})
    cmd = seen["cmd"]
    assert "--select" in cmd and cmd[cmd.index("--select") + 1] == lint_mod._SELECT
    assert "--ignore" in cmd and cmd[cmd.index("--ignore") + 1] == lint_mod._IGNORE


# --- severity + class taxonomy ----------------------------------------------

def test_injection_and_secret_rules_are_high_severity():
    """A string-concatenated SQL query and a missing zip(strict=) are not the
    same event; flattening both to medium is what gets a source ignored."""
    for code in ("S608", "S105", "S110", "S324", "S506", "S701"):
        assert lint_mod._severity_for(code) == "high", code


def test_style_and_bug_rules_stay_advisory():
    for code in ("B905", "B009", "ASYNC101", "E501"):
        assert lint_mod._severity_for(code) == "medium", code


def test_unknown_future_s_rule_defaults_to_medium_not_crash():
    """Forward-compat: a ruff upgrade adding S9xx must not raise."""
    assert lint_mod._severity_for("S999") == "medium"


def test_rule_class_is_per_family_so_precision_is_measurable():
    """Everything used to be `lint-violation`, which made 'security findings
    are accurate but style nits are noisy' invisible in the scoreboard."""
    assert lint_mod._rule_name_for("S608") == "lint-security"
    assert lint_mod._rule_name_for("B023") == "lint-bug"
    assert lint_mod._rule_name_for("ASYNC101") == "lint-async"
    assert lint_mod._rule_name_for("E501") == lint_mod._RULE


def test_finding_message_does_not_claim_fleet_adoption(monkeypatch):
    """The old text said the rule was 'already configured for the fleet'.
    True for ruff's defaults, FALSE for S/B/ASYNC - the fleet's pre-commit
    does not select them. Grug must not borrow authority it does not have."""
    results = [{
        "filename": "/tmp/x/a.py", "location": {"row": 1},
        "code": "S608", "message": "Possible SQL injection vector",
    }]
    out = lint_mod._map_results(results, "/tmp/x/", {"a.py": {1}})
    assert len(out) == 1
    assert "already configured for the fleet" not in out[0].message
    assert "noqa: S608" in out[0].message   # tells the author how to settle it
    assert out[0].severity == "high"
    assert out[0].rule_name == "lint-security"


# --- production wiring: the env var + the binary in the image --------------
#
# Everything above tests the scanner LOGIC and was already green while the
# source sat fully dead in prod (#707 follow-up, verified live 2026-08-09):
# GRUG_LINT_EVIDENCE was absent from both k8s Deployments (so _ENABLED was
# permanently False) AND ruff was not in the image (so even flipping the env
# var alone would hit the FileNotFoundError path in scan_ruff). These parse
# the REAL manifests/requirements - same pattern as test_pki_manifests.py -
# so a future edit cannot silently drop either half of the wiring again.

_ROOT = Path(__file__).resolve().parents[3]
_K8S = _ROOT / "k8s"


def _deployment_containers(manifest: str) -> list[dict]:
    docs = [d for d in yaml.safe_load_all((_K8S / manifest).read_text()) if d]
    (deploy,) = [d for d in docs if d.get("kind") == "Deployment"]
    return deploy["spec"]["template"]["spec"]["containers"]


def test_lint_evidence_enabled_on_both_review_workloads():
    """Mirrors GRUG_SAST_ENGINE (#401), which IS present in both manifests -
    the positive control proving the sibling scanner shipped correctly and
    this one was simply forgotten. Without this, _ENABLED is False in every
    pod regardless of how correct scan_ruff is."""
    for manifest in ("webhook-deployment.yaml", "consumer-deployment.yaml"):
        (container,) = _deployment_containers(manifest)
        env = {e["name"]: e.get("value") for e in container.get("env", [])}
        assert env.get("GRUG_LINT_EVIDENCE") == "ruff", (
            f"{manifest}: GRUG_LINT_EVIDENCE=ruff missing - lint evidence "
            "stays dead in prod even though the scanner is fully built"
        )


def test_ruff_binary_is_pinned_in_the_webhook_image():
    """The other half of the same gap: enabling the env var alone still
    hits scan_ruff's FileNotFoundError path (lint.py:277-284) unless ruff
    ships in the image. Pinned, not bare, matching how semgrep is pinned
    just above it in the same file - this repo ships requirements.txt
    into the image while CI installs unpinned, so an unpinned tool can
    behave differently in CI than in prod."""
    req = (_ROOT / "services" / "webhook" / "requirements.txt").read_text()
    line = next(
        (ln for ln in req.splitlines() if ln.strip().startswith("ruff")), None
    )
    assert line is not None, "ruff missing from services/webhook/requirements.txt"
    assert re.search(r"ruff[><=]=?[\d.]", line), f"ruff pin looks unpinned: {line!r}"
