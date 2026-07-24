"""Guard reopen-watch tests (2026-07-24 audit finding): the close-
completeness guard reopens an issue once and never follows up - this
persona closes that loop by escalating stale guard-reopens."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from personas.guard import reopen_watch as rw


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_is_stale_pure():
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    fresh = _iso(now - timedelta(hours=2))
    stale = _iso(now - timedelta(hours=21))
    assert rw.is_stale(fresh, now=now) is False
    assert rw.is_stale(stale, now=now) is True


def _issue(number=1, state_reason="reopened", hours_old=25, labels=None):
    now = datetime.now(timezone.utc)
    return {
        "number": number,
        "state_reason": state_reason,
        "updated_at": _iso(now - timedelta(hours=hours_old)),
        "labels": [{"name": n} for n in (labels or [])],
    }


def _wire(monkeypatch, *, enabled=True, issues=None, guard_reopened=True, already_escalated=False):
    monkeypatch.setattr(rw, "get_repo_config", lambda i, r: {"reopen_watch_enabled": enabled})
    monkeypatch.setattr(rw, "_list_open_reopened_issues", lambda t, o, r: issues or [])
    monkeypatch.setattr(rw, "_scan_issue_comments", lambda t, o, r, n: (guard_reopened, already_escalated))
    writes = []

    def _post(url, **kw):
        writes.append((url, kw.get("json")))
        return httpx.Response(201, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(rw.httpx, "post", _post)
    monkeypatch.setattr(
        rw.httpx, "get",
        lambda url, **kw: httpx.Response(200, json={}, request=httpx.Request("GET", url)),
    )
    return writes


def test_disabled_repo_costs_no_calls(monkeypatch):
    monkeypatch.setattr(rw, "get_repo_config", lambda i, r: {"reopen_watch_enabled": False})
    monkeypatch.setattr(
        rw, "_list_open_reopened_issues",
        lambda t, o, r: (_ for _ in ()).throw(AssertionError("no fetch expected")),
    )
    escalated, failed = rw.run_reopen_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert (escalated, failed) == (0, 0)


def test_stale_guard_reopen_escalates(monkeypatch):
    writes = _wire(monkeypatch, issues=[_issue(hours_old=25)])
    escalated, failed = rw.run_reopen_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert (escalated, failed) == (1, 0)
    urls = [u for u, _ in writes]
    assert any(u.endswith("/issues/1/comments") for u in urls)
    assert any(u.endswith("/issues/1/labels") for u in urls)
    comment_body = next(b for u, b in writes if u.endswith("/issues/1/comments"))["body"]
    assert rw._ESCALATION_MARKER in comment_body
    label_body = next(b for u, b in writes if u.endswith("/issues/1/labels"))
    assert label_body == {"labels": [rw._ESCALATION_LABEL]}


def test_escalate_writes_label_before_comment(monkeypatch):
    """FLINT finding, PR #744: the comment is the idempotency-
    defining write - a failure between the two writes must land BEFORE
    the comment posts, so a partial failure retries cleanly next tick
    instead of reading as permanently 'handled' with no label."""
    monkeypatch.setattr(
        rw.httpx, "get",
        lambda url, **kw: httpx.Response(200, json={}, request=httpx.Request("GET", url)),
    )
    order = []

    def _post(url, **kw):
        order.append(url)
        return httpx.Response(201, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(rw.httpx, "post", _post)
    rw._escalate("tok", "o", "r", 1, 20)
    assert order[0].endswith("/issues/1/labels")
    assert order[1].endswith("/issues/1/comments")


def test_fresh_guard_reopen_not_yet_escalated(monkeypatch):
    writes = _wire(monkeypatch, issues=[_issue(hours_old=2)])
    escalated, _ = rw.run_reopen_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert escalated == 0
    assert writes == []


def test_non_guard_reopen_ignored(monkeypatch):
    """state_reason=reopened for some OTHER reason (a human just reopened
    it) - not the guard's doing, never escalated."""
    writes = _wire(monkeypatch, issues=[_issue(hours_old=100)], guard_reopened=False)
    escalated, _ = rw.run_reopen_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert escalated == 0
    assert writes == []


def test_already_escalated_is_idempotent(monkeypatch):
    writes = _wire(monkeypatch, issues=[_issue(hours_old=100)], already_escalated=True)
    escalated, _ = rw.run_reopen_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert escalated == 0
    assert writes == []


def test_force_close_label_skips_escalation(monkeypatch):
    writes = _wire(monkeypatch, issues=[_issue(hours_old=100, labels=["force-close"])])
    escalated, _ = rw.run_reopen_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert escalated == 0
    assert writes == []


def test_wontfix_label_skips_escalation(monkeypatch):
    writes = _wire(monkeypatch, issues=[_issue(hours_old=100, labels=["wontfix"])])
    escalated, _ = rw.run_reopen_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert escalated == 0
    assert writes == []


def test_label_created_when_missing(monkeypatch):
    monkeypatch.setattr(rw, "get_repo_config", lambda i, r: {"reopen_watch_enabled": True})
    monkeypatch.setattr(rw, "_list_open_reopened_issues", lambda t, o, r: [_issue(hours_old=25)])
    monkeypatch.setattr(rw, "_scan_issue_comments", lambda t, o, r, n: (True, False))
    monkeypatch.setattr(
        rw.httpx, "get",
        lambda url, **kw: httpx.Response(404, request=httpx.Request("GET", url)),
    )
    creates = []

    def _post(url, **kw):
        if url.endswith("/labels") and "json" in kw and "name" in (kw["json"] or {}):
            creates.append(kw["json"])
            return httpx.Response(201, json={}, request=httpx.Request("POST", url))
        return httpx.Response(201, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(rw.httpx, "post", _post)
    escalated, _ = rw.run_reopen_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert escalated == 1
    assert creates and creates[0]["name"] == rw._ESCALATION_LABEL


def test_label_not_recreated_when_present(monkeypatch):
    monkeypatch.setattr(rw, "get_repo_config", lambda i, r: {"reopen_watch_enabled": True})
    monkeypatch.setattr(rw, "_list_open_reopened_issues", lambda t, o, r: [_issue(hours_old=25)])
    monkeypatch.setattr(rw, "_scan_issue_comments", lambda t, o, r, n: (True, False))
    monkeypatch.setattr(
        rw.httpx, "get",
        lambda url, **kw: httpx.Response(200, json={"name": rw._ESCALATION_LABEL}, request=httpx.Request("GET", url)),
    )
    creates = []

    def _post(url, **kw):
        if url.endswith("/labels") and (kw.get("json") or {}).get("name"):
            creates.append(kw["json"])
        return httpx.Response(201, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(rw.httpx, "post", _post)
    rw.run_reopen_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert creates == []


def test_repo_failure_does_not_abort_other_repos(monkeypatch):
    monkeypatch.setattr(rw, "get_repo_config", lambda i, r: {"reopen_watch_enabled": True})

    def _list(t, o, r):
        if r == "bad":
            raise httpx.ConnectTimeout("gh down", request=None)
        return [_issue(hours_old=25)]

    monkeypatch.setattr(rw, "_list_open_reopened_issues", _list)
    monkeypatch.setattr(rw, "_scan_issue_comments", lambda t, o, r, n: (True, False))
    monkeypatch.setattr(
        rw.httpx, "get",
        lambda url, **kw: httpx.Response(200, json={"name": rw._ESCALATION_LABEL}, request=httpx.Request("GET", url)),
    )
    monkeypatch.setattr(
        rw.httpx, "post",
        lambda url, **kw: httpx.Response(201, json={}, request=httpx.Request("POST", url)),
    )
    escalated, failed = rw.run_reopen_watch_for_install(
        "tok", 1, [{"id": 1, "full_name": "o/bad"}, {"id": 2, "full_name": "o/good"}],
    )
    assert failed == 1
    assert escalated == 1


# --- _scan_issue_comments: author-verified marker detection (FLINT --
# --- security finding, PR #744) ---------------------------------------


def _comment(body, *, login=None, app_id=None):
    c = {"body": body, "user": {"login": login} if login else {}}
    if app_id is not None:
        c["performed_via_github_app"] = {"id": app_id}
    return c


def test_scan_trusts_real_guard_reopen_comment(monkeypatch):
    monkeypatch.setattr(rw, "get_app_id", lambda: "999")
    comments = [_comment(f"{rw._GUARD_REOPEN_SNIPPET} - stuff", login="github-actions[bot]")]
    monkeypatch.setattr(
        rw.httpx, "get",
        lambda url, **kw: httpx.Response(200, json=comments, request=httpx.Request("GET", url)),
    )
    guard_reopened, already_escalated = rw._scan_issue_comments("tok", "o", "r", 1)
    assert (guard_reopened, already_escalated) == (True, False)


def test_scan_rejects_spoofed_guard_reopen_comment(monkeypatch):
    """A human (or any other app) typing the guard's literal marker text
    must NOT be trusted - only github-actions[bot] can post it for real."""
    monkeypatch.setattr(rw, "get_app_id", lambda: "999")
    comments = [_comment(f"{rw._GUARD_REOPEN_SNIPPET} - stuff", login="some-random-user")]
    monkeypatch.setattr(
        rw.httpx, "get",
        lambda url, **kw: httpx.Response(200, json=comments, request=httpx.Request("GET", url)),
    )
    guard_reopened, _ = rw._scan_issue_comments("tok", "o", "r", 1)
    assert guard_reopened is False


def test_scan_trusts_own_escalation_comment(monkeypatch):
    monkeypatch.setattr(rw, "get_app_id", lambda: "999")
    comments = [_comment(rw._ESCALATION_MARKER + "\nbody", app_id=999)]
    monkeypatch.setattr(
        rw.httpx, "get",
        lambda url, **kw: httpx.Response(200, json=comments, request=httpx.Request("GET", url)),
    )
    _, already_escalated = rw._scan_issue_comments("tok", "o", "r", 1)
    assert already_escalated is True


def test_scan_rejects_spoofed_escalation_comment(monkeypatch):
    """A decoy comment from a DIFFERENT app (or a human) containing the
    literal escalation marker must not suppress a real escalation."""
    monkeypatch.setattr(rw, "get_app_id", lambda: "999")
    comments = [_comment(rw._ESCALATION_MARKER + "\nbody", app_id=111)]
    monkeypatch.setattr(
        rw.httpx, "get",
        lambda url, **kw: httpx.Response(200, json=comments, request=httpx.Request("GET", url)),
    )
    _, already_escalated = rw._scan_issue_comments("tok", "o", "r", 1)
    assert already_escalated is False
