"""Guard hygiene watch (#655, epic #654) - the owned CI-hygiene pass.

The fleet already lints CI hygiene at DIFF time: a PR touching `.github/`
gets checked for unbounded jobs, unbounded `curl`, and floating action
refs. But a violation that merged before the linter existed sits silent
forever - nothing re-reads the DEFAULT BRANCH. This scheduled pass closes
that gap, the same way `dep_watch.py` (#491) closed it for dependency
pins: grug-poller cadence, store-driven targeting, weekly claim, ONE
quarantine-style report issue per repo, refreshed rather than duplicated.

Third instance of a template built twice already (`pulse/nudge.py` #472,
`guard/dep_watch.py` #491), so it deliberately copies their shape:
per-repo flag defaulting OFF, best-effort per repo, marker-based issue
identity, read-before-claim.

Rules mirror the diff-time linter's semantics, INCLUDING its escape
hatches - a repo that opted a line out at diff time must not be nagged
about it weekly:

  - `runs-on:` job with no `timeout-minutes:`. Reusable-caller jobs
    (top-level `uses:`, no `runs-on:`) are exempt; their timeout lives in
    the reusable. Opt out with `# hygiene: allow-no-timeout-minutes`.
  - `curl` in a `run:` block with neither a total bound (`--max-time`/`-m`)
    nor a connect bound (`--connect-timeout`). Opt out with
    `# hygiene: allow-curl-no-timeout`.
  - third-party `uses: owner/repo@ref` where ref is not a full 40-hex SHA.
    Local composites (`./.github/actions/...`) have no pinning concern.
  - dead infrastructure references in LIVE (non-comment) code.

DEAD-REFERENCE PATTERNS ARE NOT SHIPPED HERE, and that is deliberate.
The diff-time linter carries a concrete list, but those strings are one
operator's private cluster paths and grug is a PUBLIC repo - hardcoding
them would leak private infrastructure names to every reader and would be
wrong for every other install anyway. `scan_dead_refs` therefore takes
the patterns as an argument and is wired with an empty tuple until the
per-repo config surface lands (#657 pilot). The scan logic is complete
and tested; only the data is deferred.

Each scan function is pure `(path, text) -> tuple[Violation, ...]` so it
is unit-testable against fixture content with no network.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from adapters.install_store import (
    claim_hygiene_watch_report, get_repo_config, release_hygiene_watch_report,
)

log = logging.getLogger(
    f"{os.getenv('DD_SERVICE', 'grug')}.persona.guard.hygiene_watch"
)

_FETCH_TIMEOUT = 10
_MAX_FILES = 60
_MAX_REPORT_ROWS = 30
_REPORT_MARKER = "<!-- grug-guard-hygiene-watch -->"
_REPORT_TITLE = "[grug-guard] Hygiene quarantine report"

# Workflow/action YAML under .github. Scripts (.sh) carry a different rule
# set at diff time and are out of scope for this slice (#655).
_SCAN_PATH_RE = re.compile(
    r"^\.github/(?:workflows/[^/]+\.ya?ml|actions/.+\.ya?ml)$"
)

# --- rule regexes: mirrored from the diff-time linter's semantics --------
# Only owner/repo@ref refs are pin-checked; local composites are exempt.
_USES_RE = re.compile(
    r"^\s*-?\s*uses:\s*([A-Za-z0-9_-]+/[A-Za-z0-9._/-]+)@([A-Za-z0-9._-]+)"
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_CURL_RE = re.compile(r"(?:^|[\s;&|(`$])curl\s+(?:-|\\$|\"|'|\$|https?://)")
_CURL_MAXTIME_RE = re.compile(r"(?:--max-time(?:[=\s]|$)|(?:^|\s)-m(?:[=\s]|$))")
_CURL_CONNTIMEOUT_RE = re.compile(r"--connect-timeout(?:[=\s]|$)")
_CURL_ALLOW_RE = re.compile(r"#\s*hygiene:\s*allow-curl-no-timeout\b")

_JOBS_HEADER_RE = re.compile(r"^jobs:\s*$")
_JOB_KEY_RE = re.compile(r"^\s{2}([A-Za-z0-9_-]+):\s*$")
_JOB_RUNS_ON_RE = re.compile(r"^\s{4}runs-on:")
# No `_JOB_USES_RE` here on purpose: the reusable-caller exemption falls out
# of requiring `runs-on:`, since such a job carries `uses:` INSTEAD of
# `runs-on:` and is therefore never a candidate. The diff-time linter
# computes the flag and discards it; carrying that here would be dead code.
_JOB_TIMEOUT_RE = re.compile(r"^\s{4}timeout-minutes:\s*\S")
_TIMEOUT_ALLOW_RE = re.compile(r"#\s*hygiene:\s*allow-no-timeout-minutes\b")


@dataclass(frozen=True)
class Violation:
    """One hygiene finding. `category` is stable and machine-readable so the
    report can group and the scoreboard can count without parsing prose."""

    file: str
    line: int
    category: str
    detail: str


def _code_part(line: str) -> str:
    """The non-comment portion of a YAML line. Naive on purpose - it matches
    the diff-time linter, so the two scanners agree on what 'live code'
    means rather than disagreeing at the margins."""
    idx = line.find("#")
    return line if idx < 0 else line[:idx]


def _job_block_end(lines: list[str], start: int) -> int:
    """Index one past the last line of the job block beginning at `start`.

    A job's body is everything indented deeper than its 2-space key; blank
    lines do not end it. Split out of `scan_job_timeouts` so the walk and
    the rule are separately readable (Elder #766: cyclomatic 17 > cap 15).
    """
    j = start
    n = len(lines)
    while j < n:
        ln = lines[j]
        if ln.strip() == "":
            j += 1
            continue
        if len(ln) - len(ln.lstrip()) <= 2:
            break
        j += 1
    return j


def _iter_job_blocks(lines: list[str]):
    """Yield `(job_name, key_index, block_lines)` for each job in the
    `jobs:` map. A non-indented, non-blank, non-comment line ends the map.
    """
    n = len(lines)
    i = 0
    in_jobs = False
    while i < n:
        line = lines[i]
        if _JOBS_HEADER_RE.match(line):
            in_jobs = True
            i += 1
            continue
        if not in_jobs:
            i += 1
            continue
        if line.strip() and not line[0].isspace() and not line.lstrip().startswith("#"):
            in_jobs = False
            i += 1
            continue
        m = _JOB_KEY_RE.match(line)
        if not m:
            i += 1
            continue
        end = _job_block_end(lines, i + 1)
        yield m.group(1), i, lines[i:end]
        i = end


def scan_job_timeouts(path: str, text: str) -> tuple[Violation, ...]:
    """Jobs declaring `runs-on:` with no `timeout-minutes:`.

    GitHub's default is 360 minutes, so a hung step burns runner minutes
    silently. A reusable-caller job (top-level `uses:`, no `runs-on:`) is
    exempt - its bound lives in the reusable, so requiring `runs-on:`
    excludes it for free.
    """
    out: list[Violation] = []
    for job_name, key_index, block in _iter_job_blocks(text.splitlines()):
        if not any(_JOB_RUNS_ON_RE.match(b) for b in block):
            continue
        if any(_JOB_TIMEOUT_RE.match(b) for b in block):
            continue
        if any(_TIMEOUT_ALLOW_RE.search(b) for b in block):
            continue
        out.append(Violation(
            file=path, line=key_index + 1, category="job-timeout",
            detail=f"job `{job_name}` has `runs-on:` but no `timeout-minutes:`",
        ))
    return tuple(out)


def scan_curl_timeouts(path: str, text: str) -> tuple[Violation, ...]:
    """`curl` invocations bounded by neither `--max-time`/`-m` nor
    `--connect-timeout`.

    The opt-out marker is honoured on the curl line itself or the line
    directly above it, matching the diff-time linter - a repo that
    deliberately opted a line out must not be re-flagged weekly.
    """
    out: list[Violation] = []
    lines = text.splitlines()
    for idx, raw in enumerate(lines):
        code = _code_part(raw)
        if not _CURL_RE.search(code):
            continue
        if _CURL_MAXTIME_RE.search(code) or _CURL_CONNTIMEOUT_RE.search(code):
            continue
        prev = lines[idx - 1] if idx else ""
        if _CURL_ALLOW_RE.search(raw) or _CURL_ALLOW_RE.search(prev):
            continue
        out.append(Violation(
            file=path, line=idx + 1, category="curl-timeout",
            detail="curl with neither `--max-time`/`-m` nor `--connect-timeout`",
        ))
    return tuple(out)


def scan_unpinned_actions(path: str, text: str) -> tuple[Violation, ...]:
    """Third-party `uses: owner/repo@ref` where ref is not a 40-hex SHA.

    A floating tag is mutable by its publisher, so a supply-chain
    compromise lands without any diff here.
    """
    out: list[Violation] = []
    for idx, raw in enumerate(text.splitlines()):
        m = _USES_RE.match(_code_part(raw))
        if not m:
            continue
        ref = m.group(2)
        if _SHA_RE.match(ref):
            continue
        out.append(Violation(
            file=path, line=idx + 1, category="unpinned-action",
            detail=f"`{m.group(1)}@{ref}` is not pinned to a 40-hex commit SHA",
        ))
    return tuple(out)


def scan_dead_refs(
    path: str, text: str, patterns: tuple[str, ...] = (),
) -> tuple[Violation, ...]:
    """References to decommissioned infrastructure in LIVE (non-comment) code.

    `patterns` is caller-supplied and empty by default - see the module
    docstring. Comments are excluded so a note explaining a migration does
    not read as a live reference.
    """
    if not patterns:
        return ()
    out: list[Violation] = []
    for idx, raw in enumerate(text.splitlines()):
        code = _code_part(raw)
        for pat in patterns:
            if pat and pat in code:
                out.append(Violation(
                    file=path, line=idx + 1, category="dead-ref",
                    detail=f"reference to decommissioned `{pat}`",
                ))
                break
    return tuple(out)


def scan_file(
    path: str, text: str, dead_patterns: tuple[str, ...] = (),
) -> tuple[Violation, ...]:
    """Every category against one file, in stable category order."""
    return (
        scan_job_timeouts(path, text)
        + scan_curl_timeouts(path, text)
        + scan_unpinned_actions(path, text)
        + scan_dead_refs(path, text, dead_patterns)
    )


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.raw",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _discover_files(token: str, owner: str, repo: str) -> list[str]:
    """Workflow/action YAML paths across the default-branch tree. Capped,
    and truncation is LOGGED - a silent cap reads as a clean scan."""
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/trees/HEAD",
        params={"recursive": "1"},
        headers=_api_headers(token),
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    if payload.get("truncated"):
        log.info("hygiene_watch_tree_truncated", extra={"repo": f"{owner}/{repo}"})
    paths = [
        t.get("path", "") for t in payload.get("tree", [])
        if t.get("type") == "blob" and _SCAN_PATH_RE.match(t.get("path", ""))
    ]
    if len(paths) > _MAX_FILES:
        log.info(
            "hygiene_watch_file_cap",
            extra={"repo": f"{owner}/{repo}", "found": len(paths), "cap": _MAX_FILES},
        )
    return paths[:_MAX_FILES]


def _fetch_file(token: str, owner: str, repo: str, path: str) -> str | None:
    """File content at the default branch, None when absent."""
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents/{quote(path, safe='/')}",
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def _report_body(violations: list[Violation]) -> str:
    lines = [
        "Grug Guard walk the OLD trails, not just the new ones. These",
        "wounds already on the main path - nobody cut them today, so",
        "nobody saw them. Bind them before they bleed the tribe.",
        "",
        "| File | Line | Category | What Grug see |",
        "|---|---|---|---|",
    ]
    for v in violations[:_MAX_REPORT_ROWS]:
        lines.append(f"| `{v.file}` | {v.line} | `{v.category}` | {v.detail} |")
    if len(violations) > _MAX_REPORT_ROWS:
        lines.append(
            f"\n...and {len(violations) - _MAX_REPORT_ROWS} more (capped)."
        )
    lines += [
        "",
        "These are PRE-EXISTING violations on the default branch, not new",
        "ones - the diff-time linter only sees lines a PR touches. Each rule",
        "honours the same inline `# hygiene: allow-...` opt-out as the",
        "diff-time check. Re-checked weekly while hygiene-watch is enabled;",
        "this report refreshes rather than duplicates.",
        "",
        _REPORT_MARKER,
    ]
    return "\n".join(lines)


def _existing_report(token: str, owner: str, repo: str) -> int | None:
    """Open hygiene-report issue number, or None. Identified by the BODY
    MARKER, never the title - a title-substring match could overwrite an
    unrelated user issue, and a retitled bot report would duplicate
    (the #492 lesson dep_watch already paid for)."""
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues",
        params={"state": "open", "per_page": 50},
        headers=_api_headers(token),
        timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    for issue in resp.json() or []:
        if issue.get("pull_request"):
            continue
        if _REPORT_MARKER in (issue.get("body") or ""):
            return int(issue["number"])
    return None


def _write_report(
    token: str, install_id: int, owner: str, name: str,
    violations: list[Violation], existing: int | None,
) -> None:
    """File or refresh the quarantine report, and get the CLAIM semantics
    right on failure. Split out of the run loop (Elder #766: cognitive 26 >
    cap 25) - the claim rules are the subtle part and deserve to be read
    without the surrounding per-repo bookkeeping.

    A claim must represent a FILED report, not an attempt:
      - 4xx is a definite no-write -> release, so the next tick retries.
      - 5xx / transport is AMBIGUOUS (the write may have landed) -> keep
        the claim. A missed weekly report beats duplicate issues, and the
        marker-based refresh makes a later pass safe either way.
    """
    base = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}/issues"
    try:
        if existing:
            resp = httpx.patch(
                f"{base}/{existing}",
                json={"body": _report_body(violations)},
                headers=_api_headers(token), timeout=_FETCH_TIMEOUT,
            )
        else:
            resp = httpx.post(
                base,
                json={
                    "title": f"{_REPORT_TITLE} ({len(violations)} violation(s))",
                    "body": _report_body(violations),
                },
                headers=_api_headers(token), timeout=_FETCH_TIMEOUT,
            )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if 400 <= e.response.status_code < 500:
            release_hygiene_watch_report(install_id, f"{owner}/{name}")
        raise


def run_hygiene_watch_for_install(
    token: str, install_id: int, repos: list[dict[str, Any]],
) -> tuple[int, int]:
    """One hygiene-watch pass for one install's ENABLED repos. Returns
    (reports_filed, repos_failed) so the poller summary can tell a total
    outage from a clean pass. Never raises past a repo."""
    filed = 0
    failed = 0
    for repo in repos:
        repo_id = repo.get("id")
        full = repo.get("full_name", "")
        owner, _, name = full.partition("/")
        if not (repo_id and owner and name):
            continue
        try:
            cfg = get_repo_config(install_id, int(repo_id))
            if not cfg.get("guard_hygiene_watch_enabled", False):
                continue
            violations: list[Violation] = []
            for path in _discover_files(token, owner, name):
                text = _fetch_file(token, owner, name, path)
                if text:
                    violations.extend(scan_file(path, text))
            if not violations:
                log.info("hygiene_watch_clean", extra={"repo": full})
                continue
            # Read-only lookup BEFORE the claim: a read failure must not
            # burn the weekly window, and before the claim exists there is
            # nothing to release.
            existing = _existing_report(token, owner, name)
            if not claim_hygiene_watch_report(install_id, full):
                continue
            _write_report(token, install_id, owner, name, violations, existing)
            filed += 1
            log.info(
                "hygiene_watch_reported",
                extra={"install_id": install_id, "repo": full,
                       "violations": len(violations), "refreshed": bool(existing)},
            )
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            failed += 1
            log.warning(
                "hygiene_watch_repo_failed",
                extra={"install_id": install_id, "repo": full,
                       "kind": type(e).__name__},
            )
    return filed, failed
