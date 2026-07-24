"""Guard reopen-watch: escalate issues the close-completeness guard
reopened and nobody followed up on (2026-07-24 live audit finding).

The close-completeness guard (infra-public's `check.issue-close-
completeness.yml`) is a ONE-SHOT reactive tripwire: on an issue closed as
`completed` with unchecked acceptance/test boxes, it reopens the issue and
posts a single comment - then it never looks at that issue again. Live
audit of quadseven/grug found three issues (#680, #730, #736) sitting
reopened-and-ignored for 1-2+ days with zero follow-up: nobody ticked the
now-done boxes, linked the deferred ones to a follow-up issue, or closed
it as "not planned". The guard's tripwire fired correctly; nothing closed
the loop afterward.

This scheduled pass (grug-poller cadence, store-driven targeting like
Pulse/dep_watch) finds issues the guard reopened that have gone stale
(no activity since the guard's own comment, past a threshold) and
escalates ONCE: a stronger comment plus a `stale-reopened` label, so the
gap is visible without a human happening to go looking for it (the way
this one was found - a manual audit).

Default OFF per repo (`reopen_watch_enabled`), same opt-in convention as
dep_watch. Best-effort per repo; GitHub failures log and continue.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from adapters.install_store import get_repo_config

log = logging.getLogger(f"{os.getenv('DD_SERVICE', 'grug')}.persona.guard.reopen_watch")

_FETCH_TIMEOUT = 10
_MAX_ISSUE_PAGES = 3
_PER_PAGE = 100
_MAX_COMMENT_PAGES = 5
_STALE_AFTER_HOURS = int(os.getenv("GRUG_REOPEN_WATCH_STALE_HOURS", "20"))
_ESCALATION_MARKER = "<!-- grug-guard-reopen-watch -->"
_GUARD_REOPEN_SNIPPET = "Reopened by the close-completeness guard"
_ESCALATION_LABEL = "stale-reopened"
_ESCALATION_LABEL_COLOR = "b60205"
_ESCALATION_LABEL_DESC = "Reopened by the close-completeness guard and gone stale - needs a human to reconcile"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def is_stale(updated_at: str, *, now: datetime, threshold_hours: int = _STALE_AFTER_HOURS) -> bool:
    """Pure: has this issue gone untouched for >= threshold since its last
    update (the guard's own reopen bump counts as an update, so a
    genuinely-untouched issue's `updated_at` never moves past it)."""
    updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    return (now - updated) >= timedelta(hours=threshold_hours)


def _list_open_reopened_issues(token: str, owner: str, repo: str) -> list[dict[str, Any]]:
    """Open issues with state_reason == 'reopened' (REST, not Search - the
    Search API's state_reason filtering isn't reliable). Capped pages with
    a logged truncation (no silent caps, matches dep_watch's manifest
    discovery)."""
    out: list[dict[str, Any]] = []
    page = 1
    while page <= _MAX_ISSUE_PAGES:
        resp = httpx.get(
            f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues",
            params={"state": "open", "per_page": _PER_PAGE, "page": page},
            headers=_headers(token), timeout=_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json() or []
        for issue in batch:
            if issue.get("pull_request"):
                continue
            if issue.get("state_reason") == "reopened":
                out.append(issue)
        if len(batch) < _PER_PAGE:
            break
        page += 1
    else:
        log.info("reopen_watch_issue_page_cap", extra={"repo": f"{owner}/{repo}", "cap": _MAX_ISSUE_PAGES})
    return out


def _was_guard_reopened(token: str, owner: str, repo: str, issue_number: int) -> bool:
    """True if the close-completeness guard's own marker text is present
    in some comment on this issue - distinguishes a guard-reopen from any
    other reason an issue might carry state_reason=reopened."""
    page = 1
    while page <= _MAX_COMMENT_PAGES:
        resp = httpx.get(
            f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/issues/{issue_number}/comments",
            params={"per_page": _PER_PAGE, "page": page},
            headers=_headers(token), timeout=_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json() or []
        for c in batch:
            if _GUARD_REOPEN_SNIPPET in (c.get("body") or ""):
                return True
        if len(batch) < _PER_PAGE:
            return False
        page += 1
    log.info("reopen_watch_comment_page_cap", extra={"repo": f"{owner}/{repo}", "issue": issue_number})
    return False


def _already_escalated(token: str, owner: str, repo: str, issue_number: int) -> bool:
    """True if we already posted the escalation marker - escalate a given
    stale-reopen once, not every cron tick."""
    page = 1
    while page <= _MAX_COMMENT_PAGES:
        resp = httpx.get(
            f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/issues/{issue_number}/comments",
            params={"per_page": _PER_PAGE, "page": page},
            headers=_headers(token), timeout=_FETCH_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json() or []
        for c in batch:
            if _ESCALATION_MARKER in (c.get("body") or ""):
                return True
        if len(batch) < _PER_PAGE:
            return False
        page += 1
    return False


def _escalation_body(hours: int) -> str:
    return (
        f"{_ESCALATION_MARKER}\n"
        f"**Guard reopen-watch.** This issue was reopened by the close-completeness "
        f"guard over {hours}h ago and has had no activity since - it looks like the "
        f"reopen got missed.\n\n"
        f"Either finish + check the remaining boxes (linking any deferred item to its "
        f"own follow-up issue), or close this **as \"not planned\"** / add a "
        f"`force-close` label if it's no longer wanted. So speaks Grug."
    )


def _ensure_label(token: str, owner: str, repo: str) -> None:
    """Create the stale-reopened label if the repo doesn't have it yet -
    GitHub's add-labels endpoint 404s on an unknown label name rather than
    creating it, unlike the issue/PR create paths."""
    resp = httpx.get(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/labels/{quote(_ESCALATION_LABEL, safe='')}",
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    if resp.status_code == 200:
        return
    resp = httpx.post(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/labels",
        json={"name": _ESCALATION_LABEL, "color": _ESCALATION_LABEL_COLOR, "description": _ESCALATION_LABEL_DESC},
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    # 422 = raced with another tick/repo that just created it - fine.
    if resp.status_code not in (201, 422):
        resp.raise_for_status()


def _escalate(token: str, owner: str, repo: str, issue_number: int, hours: int) -> None:
    _ensure_label(token, owner, repo)
    resp = httpx.post(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/issues/{issue_number}/comments",
        json={"body": _escalation_body(hours)},
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()
    resp = httpx.post(
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/issues/{issue_number}/labels",
        json={"labels": [_ESCALATION_LABEL]},
        headers=_headers(token), timeout=_FETCH_TIMEOUT,
    )
    resp.raise_for_status()


def run_reopen_watch_for_install(
    token: str, install_id: int, repos: list[dict[str, Any]],
) -> tuple[int, int]:
    """One reopen-watch pass for one install's ENABLED repos (the
    Pulse/dep_watch store-driven targeting pattern). Returns
    (escalated, repos_failed). Never raises past a repo."""
    escalated = 0
    failed = 0
    now = datetime.now(timezone.utc)
    for repo in repos:
        repo_id = repo.get("id")
        full = repo.get("full_name", "")
        owner, _, name = full.partition("/")
        if not (repo_id and owner and name):
            continue
        try:
            if not get_repo_config(install_id, int(repo_id)).get("reopen_watch_enabled", False):
                continue
            for issue in _list_open_reopened_issues(token, owner, name):
                number = issue.get("number")
                if number is None:
                    continue
                labels = [(label.get("name") or "").lower() for label in issue.get("labels", [])]
                if "force-close" in labels or "wontfix" in labels:
                    continue
                if not is_stale(issue.get("updated_at", ""), now=now):
                    continue
                if not _was_guard_reopened(token, owner, name, number):
                    continue
                if _already_escalated(token, owner, name, number):
                    continue
                _escalate(token, owner, name, number, _STALE_AFTER_HOURS)
                escalated += 1
                log.info(
                    "reopen_watch_escalated",
                    extra={"install_id": install_id, "repo": full, "issue": number},
                )
        except (httpx.HTTPStatusError, httpx.RequestError, ValueError) as e:
            failed += 1
            log.warning(
                "reopen_watch_repo_failed",
                extra={"install_id": install_id, "repo": full, "kind": type(e).__name__},
            )
    return escalated, failed
