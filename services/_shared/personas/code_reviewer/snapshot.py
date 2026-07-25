"""Canonical identity for one Elder review input snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

# v2: intent text is normalized (HTML comments + whitespace noise stripped)
# so bot footers and auto-generated body rewrites no longer thrash mid-flight
# Elder reviews while human intent is unchanged.
_SNAPSHOT_VERSION = "elder-review-v2"

# HTML comments are almost always bot/tool footers (release notes blocks,
# metadata markers). They rewrite without changing the author's intent and
# used to force a brand-new snapshot_id -> mid-flight cancel storm.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MULTI_BLANK = re.compile(r"\n{3,}")


def normalize_intent_text(text: str) -> str:
    """Return intent text suitable for snapshot identity.

    Strips HTML comments and collapses blank-line noise. Real human prose
    (title and body) still participates in the hash after cleanup so an
    author rewriting the Why still re-triggers review.
    """
    cleaned = _HTML_COMMENT.sub("", text or "")
    cleaned = _MULTI_BLANK.sub("\n\n", cleaned)
    return cleaned.strip()


def review_snapshot_id(
    *,
    base_sha: str,
    head_sha: str,
    title: str,
    body: str,
) -> str:
    """Return an unambiguous, bounded identity for every reviewed input.

    Head SHA alone is insufficient: changing the base changes the diff, while
    changing title/body changes the intent supplied to the reviewer. JSON
    array encoding preserves field boundaries and exact text without relying
    on delimiter escaping. Intent fields are normalized first so ephemeral
    bot footers do not thrash the durable review lane.
    """
    material = json.dumps(
        [
            _SNAPSHOT_VERSION,
            base_sha,
            head_sha,
            normalize_intent_text(title),
            normalize_intent_text(body),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"v1:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def review_snapshot_id_from_pr(pr: Mapping[str, Any]) -> str:
    """Build the canonical identity from GitHub pull-request JSON."""
    return review_snapshot_id(
        base_sha=str((pr.get("base") or {}).get("sha") or ""),
        head_sha=str((pr.get("head") or {}).get("sha") or ""),
        title=str(pr.get("title") or ""),
        body=str(pr.get("body") or ""),
    )


def review_freshness_id(
    *,
    head_sha: str,
    title: str,
    body: str,
) -> str:
    """Identity for "is an in-flight review still worth publishing?".

    Deliberately EXCLUDES base_sha, which is the one input the PR author does
    not control and which changes constantly on a busy repo.

    `base_sha` is `pr.base.sha`, the base branch TIP. It moves every time ANY
    other PR merges to the base branch. Because `review_snapshot_id` hashes it,
    a single merge to main changed the snapshot_id of EVERY open PR, which made
    every in-flight Elder review look stale, which cancelled it, which
    re-enqueued it against the same head - and on a repo with a busy afternoon
    that loop never converges.

    Measured on quadseven/infra 2026-07-25: eight merges in one session, and
    Elder failed to publish on multiple PRs with `reviewed_head_sha ==
    current_head_sha` and only the snapshot differing. The user-facing error
    even read "superseded by a newer commit" when no new commit existed.

    What still invalidates a review, and should:
      * head_sha  - the author pushed new code
      * title     - stated intent changed
      * body      - stated intent changed

    What no longer does:
      * base_sha  - somebody else merged something unrelated

    The trade-off is explicit: a base move CAN change the effective diff
    (GitHub diffs against the merge base). Publishing a review computed against
    a slightly older base is a small, bounded staleness. NEVER publishing one is
    unbounded. Elder is advisory, so the former is clearly the better failure
    mode - and a real base change that touches the same lines will surface on
    the next push anyway.
    """
    material = json.dumps(
        [
            _SNAPSHOT_VERSION,
            head_sha,
            normalize_intent_text(title),
            normalize_intent_text(body),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"v1:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def review_freshness_id_from_pr(pr: Mapping[str, Any]) -> str:
    """Build the base-insensitive freshness identity from GitHub PR JSON."""
    return review_freshness_id(
        head_sha=str((pr.get("head") or {}).get("sha") or ""),
        title=str(pr.get("title") or ""),
        body=str(pr.get("body") or ""),
    )


def adaptive_elder_settle_seconds(
    pr: Mapping[str, Any],
    *,
    base_seconds: int,
) -> int:
    """Scale the quiet window to the size of the hunt (Swift Elder).

    Tiny PRs almost never get force-pushed mid-settle; waiting the full
    quiet window only adds empty latency. Large multi-file PRs keep the
    full base window so rapid push storms do not burn dual Cave arms on
    every intermediate head.

    When GitHub omits size stats (additions/deletions/changed_files all
    zero/absent), keep ``base_seconds`` — never invent a "tiny" path from
    missing data.
    """
    base = max(0, int(base_seconds))
    try:
        additions = int(pr.get("additions") or 0)
        deletions = int(pr.get("deletions") or 0)
        changed = int(pr.get("changed_files") or 0)
    except (TypeError, ValueError):
        return base
    churn = max(0, additions) + max(0, deletions)
    changed = max(0, changed)
    if changed == 0 and churn == 0:
        return base
    # Swift Hunt: small, focused PR — start review immediately.
    if changed <= 5 and churn <= 120:
        return 0
    # Steady Hunt: medium PR — short settle only.
    if changed <= 12 and churn <= 400:
        return min(base, 3)
    # Full Hunt: large / noisy PR — full quiet window.
    return base
