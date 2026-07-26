"""Tests for the ruff lint-evidence source (#681, epic #707).

The source is OFF by default (GRUG_LINT_EVIDENCE), so every test that expects
findings enables it explicitly - and one test pins the default-off contract.
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

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
