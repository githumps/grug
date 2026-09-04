"""Chief's linked-issue fetcher - the ONE impure builder behind
`check_linked_issue_completeness` (#782).

`evaluate_pull_request` stays pure (spec 0002): the fetcher is a parameter
it receives, never a call it makes. Every caller that evaluates a PR for
real must pass a fetcher built HERE - the `pull_request` webhook dispatch
and the `/grug recheck` comment path both do. Pre-#782 only the webhook
path built one (inline, in `webhook_dispatch.py`); the recheck path called
`evaluate_pull_request(pr_body)` bare, so its `linked-issue-completeness`
check structurally hit the no-fetcher fail-open branch and a stale red
row could be turned green by a comment without the check ever running.
One builder, imported by both, is what keeps the two paths from
diverging again.

Imports stay inside the builder: the ACK path pays import cost only when
a persona actually dispatches, and the `github_app_auth` patch target
the tests intercept keeps working.
"""
from __future__ import annotations

from personas.tpm.dor_checks import IssueFetcher


def build_issue_fetcher(
    *, installation_id: int, owner: str, repo: str,
) -> IssueFetcher:
    """Return a `number -> issue body` callable reading via the install
    token. Raises on any fetch failure (HTTP status, transport, auth) -
    `check_linked_issue_completeness` catches that and fails OPEN with the
    check marked `skipped`, so a GitHub blip never blocks merges and never
    masquerades as a pass either."""

    def _fetch_issue_body(number: int) -> str:
        from urllib.parse import quote
        import httpx  # type: ignore

        def _get(token: str) -> str:
            r = httpx.get(
                f"https://api.github.com/repos/{quote(owner, safe='')}/"
                f"{quote(repo, safe='')}/issues/{int(number)}",
                headers={
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=10,
            )
            r.raise_for_status()
            return (r.json() or {}).get("body") or ""

        from github_app_auth import with_install_token_retry  # type: ignore
        return with_install_token_retry(installation_id, _get)

    return _fetch_issue_body
