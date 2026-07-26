"""Deterministic lint evidence for Elder reviews (#681, epic #707).

#707 measured Elder's RECALL at 1.8% against the corpus, and 0 of 24
critical-severity findings. Precision work (#708/#779) removes false
markings but adds no coverage. This module adds coverage the model cannot
reliably reproduce: a real linter over the changed files, whose findings
never hallucinate because they are not generated - they are measured.

Ruff first, because it is already the fleet's Python linter (provisioned to
every repo via #995's standard `.pre-commit-config.yaml`), so a finding here
is a rule the repo has ALREADY adopted rather than an opinion Elder is
importing. That makes the marking arguable-with rather than noise.

WHY THIS IS NOT sast.py. `scan_semgrep` produces `Candidate`s that go
through the exploitability judge, which is correct for security findings.
Lint findings are not security claims and must not be judged for
exploitability - they are deterministic and precise by construction. So this
mirrors `scan_complexity` instead: it returns `Finding`s directly, and
dispatch merges them via `with_extra_findings` AFTER the verification pass
and refute gate, both of which exist to police MODEL judgment.

THE RUFF EXIT-CODE TRAP. Ruff exits **1 when it finds violations** - that is
success, not failure. Copying `scan_semgrep`'s `returncode != 0 -> return ()`
would discard every real result and degrade to a permanent silent zero, which
is the exact shape of the semgrep HOME-dir bug found live on 2026-07-13. Only
exit >= 2 is a genuine ruff error. Cache is disabled outright (`--no-cache`)
rather than repointing HOME, since the pods run readOnlyRootFilesystem as uid
10001 with --no-create-home and a cache write is the thing that crashed
semgrep there.

Diff-anchored like every other source: only violations on lines THIS PR
ADDED are reported, so Elder flags what the PR introduces rather than
pre-existing debt the author did not touch.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile

from personas.code_reviewer.diff_parser import DiffHunk
from personas.code_reviewer.persona import Finding
# Reuse the canonical added-lines walk rather than a third copy of it
# (standard-pattern-reuse-or-elect): complexity._changed_by_file already
# walks the unified diff body and returns new-side ADDED line numbers.
from personas.code_reviewer.complexity import _changed_by_file

log = logging.getLogger("grug.code_reviewer.lint")

# OFF by default. A new finding source fires on EVERY PR fleet-wide, and the
# #707 lesson is that a speculative source is worse than none. Enable per
# environment once the acceptance rate is observed:
#   GRUG_LINT_EVIDENCE=ruff
# Mirrors sast.py's GRUG_SAST_ENGINE gate rather than inventing a new
# mechanism (standard-pattern-reuse-or-elect).
_ENABLED = os.getenv("GRUG_LINT_EVIDENCE", "").strip().lower() == "ruff"

_RULE = "lint-violation"

# Advisory, same as complexity: a lint nit must never be the thing that
# blocks a merge on its own.
_SEVERITY = "medium"

_TIMEOUT_S = 30
_MAX_SCAN_BYTES = 1_000_000
_MAX_FINDINGS = 20

# Ruff exit codes: 0 = clean, 1 = violations found (SUCCESS for us),
# >= 2 = ruff itself failed. See the module docstring.
_RUFF_ERROR_EXIT = 2


def _budget(file_contents: dict[str, str]) -> dict[str, str]:
    """Python files only, under a total byte budget, no path escapes."""
    kept: dict[str, str] = {}
    total = 0
    for path, text in sorted(file_contents.items()):
        if not path.endswith(".py"):
            continue
        if os.path.isabs(path) or ".." in path.split("/"):
            log.warning("lint_path_escape_skipped", extra={"path": path})
            continue
        size = len(text.encode("utf-8", "replace"))
        if total + size > _MAX_SCAN_BYTES:
            continue
        kept[path] = text
        total += size
    return kept


def _write(tmp: str, files: dict[str, str]) -> None:
    for path, text in files.items():
        dest = os.path.join(tmp, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(text)


def scan_ruff(
    hunks: tuple[DiffHunk, ...], file_contents: dict[str, str]
) -> tuple[Finding, ...]:
    """Ruff over the changed Python files, diff-anchored to added lines.

    Best-effort by contract: a missing binary, a real ruff error, a timeout or
    unparseable output all return () and log. Lint evidence is additive and
    must never break a review.
    """
    if not _ENABLED:
        return ()
    if not file_contents:
        return ()
    kept = _budget(file_contents)
    if not kept:
        return ()
    added = _changed_by_file(hunks)
    if not added:
        return ()

    try:
        with tempfile.TemporaryDirectory(prefix="grug-lint-") as tmp:
            _write(tmp, kept)
            proc = subprocess.run(
                ["ruff", "check", "--output-format", "json", "--no-cache", tmp],
                capture_output=True, text=True, timeout=_TIMEOUT_S,
                check=False,
            )
            # ONLY >= 2 is an error. Exit 1 means violations were found and
            # stdout carries them - see the module docstring.
            if proc.returncode >= _RUFF_ERROR_EXIT:
                log.warning(
                    "lint_ruff_run_failed",
                    extra={
                        "returncode": proc.returncode,
                        "stderr": (proc.stderr or "")[-2000:],
                    },
                )
                return ()
            results = json.loads(proc.stdout or "[]")
            prefix = tmp.rstrip("/") + "/"
    except FileNotFoundError:
        log.info("lint_ruff_binary_missing")
        return ()
    except subprocess.TimeoutExpired:
        log.warning("lint_ruff_timeout", extra={"timeout_s": _TIMEOUT_S})
        return ()
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("lint_ruff_unparseable", extra={"kind": type(e).__name__})
        return ()

    return _map_results(results, prefix, added)


def _map_results(
    results: object, prefix: str, added: dict[str, set[int]]
) -> tuple[Finding, ...]:
    """Map ruff JSON to diff-anchored Findings. Tolerant of shape drift: a
    result missing a field is skipped, never fatal."""
    if not isinstance(results, list):
        return ()
    findings: list[Finding] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        path = str(r.get("filename") or "")
        if path.startswith(prefix):
            path = path[len(prefix):]
        loc = r.get("location") or {}
        row = loc.get("row") if isinstance(loc, dict) else None
        code = r.get("code")
        msg = r.get("message")
        if not path or not isinstance(row, int) or not code or not msg:
            continue
        if row not in added.get(path, set()):
            continue  # pre-existing violation on an untouched line
        findings.append(
            Finding(
                file=path,
                line=row,
                severity=_SEVERITY,
                rule_name=_RULE,
                message=(
                    f"ruff `{code}`: {msg}. Deterministic lint finding - this "
                    f"rule is already configured for the fleet, so it is not "
                    f"an opinion Elder is importing."
                ),
                suggestion=None,
                effort=None,
            )
        )
        if len(findings) >= _MAX_FINDINGS:
            log.info("lint_ruff_findings_capped", extra={"cap": _MAX_FINDINGS})
            break
    return tuple(findings)
