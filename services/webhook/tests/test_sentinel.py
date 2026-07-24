"""Sentinel persona tests (grug#721 follow-up, epic #707) - flags PRs
closed while Elder's last review verdict was still blocking. Mirrors the
Warder/Pulse test shape (test_warder_pulse.py): dispatch-gate tests via a
built PullRequestContext, monkeypatched I/O.
"""
from __future__ import annotations

import httpx

from personas.sentinel import webhook_dispatch as sentinel


def _ctx(payload, *, head_sha="headsha"):
    from personas.registry import PullRequestContext

    return PullRequestContext(
        installation_id=1, owner="o", repo_name="r", head_sha=head_sha,
        pr_number=5, pr_body="", payload=payload, delivery_id="d", blocking=False,
    )


def _verdict(*, blocking, findings_count=2, summary="secret-in-log-or-trace"):
    return {
        "persona": "elder", "repo": "o/r", "pr_number": 5, "head_sha": "headsha",
        "conclusion": "failure" if blocking else "success", "summary": summary,
        "findings_count": findings_count, "blocking": blocking,
        "verdict": "block" if blocking else "pass", "created_at": "2026-07-21T00:00:00Z",
    }


def _no_candidates(monkeypatch):
    """No high/critical CommentRecords for this PR - the abandoned-finding
    check has nothing to look at, matching v1 behavior exactly."""
    monkeypatch.setattr(sentinel, "_high_severity_records", lambda iid, o, r, pr: [])


def test_skips_when_elder_never_reviewed(monkeypatch):
    monkeypatch.setattr(sentinel, "get_check_verdict", lambda iid, sha, persona: None)
    _no_candidates(monkeypatch)
    monkeypatch.setattr(
        sentinel, "with_install_token_retry",
        lambda iid, fn: (_ for _ in ()).throw(AssertionError("no GitHub call expected")),
    )
    out = sentinel.dispatch_pull_request(_ctx({"pull_request": {"merged": False}}))
    assert out == {"persona": "sentinel", "result": "skipped"}


def test_skips_when_last_verdict_not_blocking_and_no_high_severity_records(monkeypatch):
    monkeypatch.setattr(sentinel, "get_check_verdict", lambda iid, sha, persona: _verdict(blocking=False))
    _no_candidates(monkeypatch)
    monkeypatch.setattr(
        sentinel, "with_install_token_retry",
        lambda iid, fn: (_ for _ in ()).throw(AssertionError("no GitHub call expected")),
    )
    out = sentinel.dispatch_pull_request(_ctx({"pull_request": {"merged": True}}))
    assert out == {"persona": "sentinel", "result": "skipped"}


def test_flags_unmerged_close_with_blocking_verdict(monkeypatch):
    monkeypatch.setattr(sentinel, "get_check_verdict", lambda iid, sha, persona: _verdict(blocking=True))
    _no_candidates(monkeypatch)
    monkeypatch.setattr(sentinel, "_find_marker_comment", lambda token, o, r, pr: None)
    posted = []
    monkeypatch.setattr(
        sentinel.httpx, "post",
        lambda url, **kw: posted.append(kw["json"]["body"]) or httpx.Response(
            201, request=httpx.Request("POST", url), json={},
        ),
    )
    monkeypatch.setattr(sentinel, "with_install_token_retry", lambda iid, fn: fn("tok"))
    verdicts = []
    monkeypatch.setattr(sentinel, "record_check_verdict", lambda **kw: verdicts.append(kw))

    out = sentinel.dispatch_pull_request(_ctx({"pull_request": {"merged": False}}))

    assert out == {"persona": "sentinel", "result": "flagged"}
    assert len(posted) == 1
    assert sentinel.MARKER in posted[0]
    assert "closed without merging" in posted[0]
    assert "secret-in-log-or-trace" in posted[0]
    assert verdicts[0]["persona_key"] == "sentinel"
    assert verdicts[0]["blocking"] is False  # advisory only, never gates
    assert verdicts[0]["findings_count"] == 2


def test_flags_merge_with_blocking_verdict_as_shipped(monkeypatch):
    """A merge despite a failing non-required check is the WORSE outcome
    (the finding shipped) - the comment must say so, distinct wording
    from the unmerged-close case."""
    monkeypatch.setattr(sentinel, "get_check_verdict", lambda iid, sha, persona: _verdict(blocking=True))
    _no_candidates(monkeypatch)
    monkeypatch.setattr(sentinel, "_find_marker_comment", lambda token, o, r, pr: None)
    posted = []
    monkeypatch.setattr(
        sentinel.httpx, "post",
        lambda url, **kw: posted.append(kw["json"]["body"]) or httpx.Response(
            201, request=httpx.Request("POST", url), json={},
        ),
    )
    monkeypatch.setattr(sentinel, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(sentinel, "record_check_verdict", lambda **kw: None)

    out = sentinel.dispatch_pull_request(_ctx({"pull_request": {"merged": True}}))

    assert out == {"persona": "sentinel", "result": "flagged"}
    assert "SHIPPED" in posted[0]


def test_already_flagged_posts_nothing_twice(monkeypatch):
    monkeypatch.setattr(sentinel, "get_check_verdict", lambda iid, sha, persona: _verdict(blocking=True))
    _no_candidates(monkeypatch)
    monkeypatch.setattr(sentinel, "_find_marker_comment", lambda token, o, r, pr: 999)
    monkeypatch.setattr(
        sentinel.httpx, "post",
        lambda url, **kw: (_ for _ in ()).throw(AssertionError("no POST expected")),
    )
    monkeypatch.setattr(sentinel, "with_install_token_retry", lambda iid, fn: fn("tok"))
    verdicts = []
    monkeypatch.setattr(sentinel, "record_check_verdict", lambda **kw: verdicts.append(kw))

    out = sentinel.dispatch_pull_request(_ctx({"pull_request": {"merged": False}}))

    assert out == {"persona": "sentinel", "result": "already_flagged"}
    assert verdicts == []  # no duplicate Activity-feed row either


def test_publish_failure_degrades_without_raising(monkeypatch):
    monkeypatch.setattr(sentinel, "get_check_verdict", lambda iid, sha, persona: _verdict(blocking=True))
    _no_candidates(monkeypatch)
    monkeypatch.setattr(
        sentinel, "with_install_token_retry",
        lambda iid, fn: (_ for _ in ()).throw(httpx.ConnectTimeout("gh down", request=None)),
    )
    out = sentinel.dispatch_pull_request(_ctx({"pull_request": {"merged": False}}))
    assert out == {"persona": "sentinel", "result": "publish_failed"}


# --- grug#743 extension: abandoned high/critical findings from an EARLIER --
# --- Living Hunt pass, even when the LAST verdict is clean ------------------


def _record(comment_id=1, severity="high"):
    return {
        "comment_id": comment_id, "repo": "o/r", "pr_number": 5,
        "review_span_context": None, "finding_tags": {"severity": severity},
    }


def test_flags_abandoned_finding_even_when_last_verdict_clean(monkeypatch):
    """The exact grug#679 pattern: last verdict is clean (a later push's
    delta didn't touch the flagged lines again), but an EARLIER
    high-severity finding never got a human reply."""
    monkeypatch.setattr(sentinel, "get_check_verdict", lambda iid, sha, persona: _verdict(blocking=False))
    monkeypatch.setattr(sentinel, "_high_severity_records", lambda iid, o, r, pr: [_record()])
    monkeypatch.setattr(sentinel, "_replied_to_comment_ids", lambda token, o, r, pr: set())
    monkeypatch.setattr(sentinel, "_find_marker_comment", lambda token, o, r, pr: None)
    posted = []
    monkeypatch.setattr(
        sentinel.httpx, "post",
        lambda url, **kw: posted.append(kw["json"]["body"]) or httpx.Response(
            201, request=httpx.Request("POST", url), json={},
        ),
    )
    monkeypatch.setattr(sentinel, "with_install_token_retry", lambda iid, fn: fn("tok"))
    verdicts = []
    monkeypatch.setattr(sentinel, "record_check_verdict", lambda **kw: verdicts.append(kw))

    out = sentinel.dispatch_pull_request(_ctx({"pull_request": {"merged": True}}))

    assert out == {"persona": "sentinel", "result": "flagged"}
    assert "Living Hunt" in posted[0]
    assert "never got a reply" in posted[0]
    assert verdicts[0]["findings_count"] == 1


def test_skips_when_high_severity_finding_has_a_human_reply(monkeypatch):
    monkeypatch.setattr(sentinel, "get_check_verdict", lambda iid, sha, persona: _verdict(blocking=False))
    monkeypatch.setattr(sentinel, "_high_severity_records", lambda iid, o, r, pr: [_record()])
    monkeypatch.setattr(sentinel, "_replied_to_comment_ids", lambda token, o, r, pr: {1})
    monkeypatch.setattr(sentinel, "with_install_token_retry", lambda iid, fn: fn("tok"))
    monkeypatch.setattr(
        sentinel.httpx, "post",
        lambda url, **kw: (_ for _ in ()).throw(AssertionError("no POST expected")),
    )
    out = sentinel.dispatch_pull_request(_ctx({"pull_request": {"merged": True}}))
    assert out == {"persona": "sentinel", "result": "skipped"}


def test_reply_scan_failure_falls_back_to_last_verdict_only(monkeypatch):
    """A GitHub hiccup on the reply-scan must not sink a genuine
    last-verdict-blocking flag - best-effort, degrades gracefully."""
    monkeypatch.setattr(sentinel, "get_check_verdict", lambda iid, sha, persona: _verdict(blocking=True))
    monkeypatch.setattr(sentinel, "_high_severity_records", lambda iid, o, r, pr: [_record()])

    calls = {"n": 0}

    def _retry(iid, fn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("gh down", request=None)
        return fn("tok")

    monkeypatch.setattr(sentinel, "with_install_token_retry", _retry)
    monkeypatch.setattr(sentinel, "_find_marker_comment", lambda token, o, r, pr: None)
    posted = []
    monkeypatch.setattr(
        sentinel.httpx, "post",
        lambda url, **kw: posted.append(kw["json"]["body"]) or httpx.Response(
            201, request=httpx.Request("POST", url), json={},
        ),
    )
    monkeypatch.setattr(sentinel, "record_check_verdict", lambda **kw: None)

    out = sentinel.dispatch_pull_request(_ctx({"pull_request": {"merged": False}}))
    assert out == {"persona": "sentinel", "result": "flagged"}
    assert "closed without merging" in posted[0]  # last-verdict wording, not abandoned_only


def test_anchors_on_pr_head_sha_not_merge_commit(monkeypatch):
    """Unlike Warder (which anchors on merge_commit_sha for its own
    release-changelog purpose), Sentinel must look up Elder's verdict by
    the PR HEAD sha - that's what Elder actually reviewed."""
    seen = {}

    def fake_get_check_verdict(iid, sha, persona):
        seen["sha"] = sha
        seen["persona"] = persona
        return None

    monkeypatch.setattr(sentinel, "get_check_verdict", fake_get_check_verdict)
    _no_candidates(monkeypatch)
    sentinel.dispatch_pull_request(_ctx(
        {"pull_request": {"merged": True, "merge_commit_sha": "mergesha"}},
        head_sha="reviewedsha",
    ))
    assert seen == {"sha": "reviewedsha", "persona": "elder"}


# ── Registry: sentinel action seam ─────────────────────────────────────


def test_sentinel_registered_on_closed_action_enabled_by_default():
    """Safety net, not an opt-in tracer (unlike Warder/Pulse/Smasher) -
    enabled_default=True + missing_repo_policy='enabled', same stance as
    Elder, and non-blocking since the PR is already closed by the time
    this fires."""
    from personas import registry

    spec = registry.by_key("sentinel")
    assert spec.actions == ("closed",)
    assert spec.enabled_default is True
    assert spec.missing_repo_policy == "enabled"
    assert spec.blocking_flag is None
    assert spec.check_run_name == "Grug - Sentinel"
