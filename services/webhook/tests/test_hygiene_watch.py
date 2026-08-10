"""Guard hygiene watch tests (#655, epic #654) - the owned CI-hygiene pass.

Scan functions are pure `(path, text) -> tuple[Violation, ...]`, so every
rule is exercised against fixture content with no network. The run-loop
tests mirror test_dep_watch.py's `_wire` shape.
"""
from __future__ import annotations

import httpx

from personas.guard import hygiene_watch as hw


# --- scan_job_timeouts ---------------------------------------------------

def test_job_with_runs_on_and_no_timeout_is_flagged():
    v = hw.scan_job_timeouts("wf.yml", "\n".join([
        "jobs:",
        "  build:",
        "    runs-on: ubuntu-latest",
        "    steps:",
        "      - run: echo hi",
    ]))
    assert [(x.line, x.category) for x in v] == [(2, "job-timeout")]
    assert "build" in v[0].detail


def test_job_with_timeout_is_clean():
    assert hw.scan_job_timeouts("wf.yml", "\n".join([
        "jobs:",
        "  build:",
        "    runs-on: ubuntu-latest",
        "    timeout-minutes: 10",
    ])) == ()


def test_reusable_caller_job_is_exempt():
    """A job carrying top-level `uses:` instead of `runs-on:` gets its bound
    from the reusable - flagging it would be noise the diff-time linter
    does not produce either."""
    assert hw.scan_job_timeouts("wf.yml", "\n".join([
        "jobs:",
        "  call:",
        "    uses: o/r/.github/workflows/x.yml@abc",
        "    secrets: inherit",
    ])) == ()


def test_job_timeout_opt_out_marker_is_honoured():
    assert hw.scan_job_timeouts("wf.yml", "\n".join([
        "jobs:",
        "  build:  # hygiene: allow-no-timeout-minutes long migration",
        "    runs-on: ubuntu-latest",
    ])) == ()


def test_multiple_jobs_each_evaluated_independently():
    v = hw.scan_job_timeouts("wf.yml", "\n".join([
        "jobs:",
        "  a:",
        "    runs-on: ubuntu-latest",
        "    timeout-minutes: 5",
        "  b:",
        "    runs-on: ubuntu-latest",
        "  c:",
        "    runs-on: ubuntu-latest",
        "    timeout-minutes: 5",
    ]))
    assert [x.line for x in v] == [5]


def test_content_after_jobs_map_ends_scanning():
    """A top-level key after `jobs:` closes the map; a 2-space key under it
    is not a job and must not be evaluated."""
    assert hw.scan_job_timeouts("wf.yml", "\n".join([
        "jobs:",
        "  build:",
        "    runs-on: ubuntu-latest",
        "    timeout-minutes: 1",
        "concurrency:",
        "  group:",
    ])) == ()


# --- scan_curl_timeouts --------------------------------------------------

def test_unbounded_curl_is_flagged():
    v = hw.scan_curl_timeouts("wf.yml", "      - run: curl -sSL https://example.com")
    assert [(x.line, x.category) for x in v] == [(1, "curl-timeout")]


def test_curl_with_max_time_is_clean():
    assert hw.scan_curl_timeouts("wf.yml", "  run: curl --max-time 10 https://x") == ()


def test_curl_with_connect_timeout_is_clean():
    assert hw.scan_curl_timeouts("wf.yml", "  run: curl --connect-timeout 5 https://x") == ()


def test_curl_short_m_flag_is_clean():
    assert hw.scan_curl_timeouts("wf.yml", "  run: curl -m 5 https://x") == ()


def test_curl_opt_out_on_same_or_previous_line():
    same = "  run: curl -sSL https://x  # hygiene: allow-curl-no-timeout installer"
    assert hw.scan_curl_timeouts("wf.yml", same) == ()
    above = "\n".join([
        "  # hygiene: allow-curl-no-timeout installer one-liner",
        "  run: curl -sSL https://x",
    ])
    assert hw.scan_curl_timeouts("wf.yml", above) == ()


def test_curl_mentioned_in_a_comment_is_not_a_violation():
    """`_code_part` strips comments, so prose about curl is not code."""
    assert hw.scan_curl_timeouts("wf.yml", "  # wraps the curl block below") == ()


# --- scan_unpinned_actions -----------------------------------------------

def test_tag_pinned_action_is_flagged():
    v = hw.scan_unpinned_actions("wf.yml", "      - uses: actions/checkout@v4")
    assert [(x.line, x.category) for x in v] == [(1, "unpinned-action")]
    assert "actions/checkout@v4" in v[0].detail


def test_sha_pinned_action_is_clean():
    sha = "a" * 40
    assert hw.scan_unpinned_actions("wf.yml", f"      - uses: actions/checkout@{sha}") == ()


def test_local_composite_action_has_no_pinning_concern():
    assert hw.scan_unpinned_actions("wf.yml", "      - uses: ./.github/actions/setup") == ()


def test_short_sha_is_not_accepted_as_pinned():
    assert len(hw.scan_unpinned_actions("wf.yml", "      - uses: o/r@abc1234")) == 1


# --- scan_dead_refs ------------------------------------------------------

def test_dead_refs_empty_patterns_is_a_no_op():
    """Ships empty on purpose - the patterns are one operator's private
    infrastructure names and this is a public repo."""
    assert hw.scan_dead_refs("wf.yml", "kubeconfig: /old/cluster/path") == ()


def test_dead_ref_in_live_code_is_flagged():
    v = hw.scan_dead_refs("wf.yml", "  path: /old/cluster", patterns=("/old/cluster",))
    assert [(x.line, x.category) for x in v] == [(1, "dead-ref")]


def test_dead_ref_in_a_comment_is_not_flagged():
    assert hw.scan_dead_refs(
        "wf.yml", "  # migrated away from /old/cluster in 2026", patterns=("/old/cluster",),
    ) == ()


def test_dead_ref_reports_one_violation_per_line():
    v = hw.scan_dead_refs("wf.yml", "  a: /x /y", patterns=("/x", "/y"))
    assert len(v) == 1


# --- scan_file -----------------------------------------------------------

def test_scan_file_aggregates_every_category():
    v = hw.scan_file("wf.yml", "\n".join([
        "jobs:",
        "  build:",
        "    runs-on: ubuntu-latest",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "      - run: curl -sSL https://x",
    ]))
    assert {x.category for x in v} == {"job-timeout", "unpinned-action", "curl-timeout"}


def test_scan_file_passes_dead_patterns_through():
    """The `dead_patterns` argument scan_file has always accepted must
    actually reach scan_dead_refs when a caller supplies it."""
    v = hw.scan_file("wf.yml", "  path: /old/cluster", ("/old/cluster",))
    assert [(x.line, x.category) for x in v] == [(1, "dead-ref")]


# --- run loop ------------------------------------------------------------

_DIRTY = "\n".join([
    "jobs:",
    "  build:",
    "    runs-on: ubuntu-latest",
])


def _wire(monkeypatch, *, enabled=True, content=_DIRTY, existing_report=None,
          claim=True, dead_ref_patterns=()):
    monkeypatch.setattr(
        hw, "get_repo_config",
        lambda i, r: {
            "guard_hygiene_watch_enabled": enabled,
            "guard_hygiene_dead_ref_patterns": dead_ref_patterns,
        },
    )
    monkeypatch.setattr(hw, "_discover_files", lambda t, o, r: [".github/workflows/ci.yml"])
    monkeypatch.setattr(hw, "_fetch_file", lambda t, o, r, p: content)
    monkeypatch.setattr(hw, "claim_hygiene_watch_report", lambda i, r: claim)
    monkeypatch.setattr(hw, "_existing_report", lambda t, o, r: existing_report)
    writes = []
    monkeypatch.setattr(
        hw.httpx, "post",
        lambda url, **kw: writes.append(("post", url, kw.get("json"))) or httpx.Response(
            201, json={"number": 9}, request=httpx.Request("POST", url)),
    )
    monkeypatch.setattr(
        hw.httpx, "patch",
        lambda url, **kw: writes.append(("patch", url, kw.get("json"))) or httpx.Response(
            200, json={}, request=httpx.Request("PATCH", url)),
    )
    return writes


def test_violations_file_one_report_issue(monkeypatch):
    writes = _wire(monkeypatch)
    filed, failed = hw.run_hygiene_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert (filed, failed) == (1, 0)
    assert len(writes) == 1
    verb, url, body = writes[0]
    assert verb == "post" and url.endswith("/issues")
    assert "Hygiene quarantine report" in body["title"]
    assert "job-timeout" in body["body"]
    assert hw._REPORT_MARKER in body["body"]


def test_same_week_rerun_updates_the_same_issue(monkeypatch):
    writes = _wire(monkeypatch, existing_report=7)
    filed, failed = hw.run_hygiene_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert (filed, failed) == (1, 0)
    verb, url, _ = writes[0]
    assert verb == "patch" and url.endswith("/issues/7")


def test_clean_repo_files_no_issue(monkeypatch):
    clean = "jobs:\n  build:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n"
    writes = _wire(monkeypatch, content=clean)
    assert hw.run_hygiene_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == (0, 0)
    assert writes == []


def test_disabled_repo_costs_no_fetch(monkeypatch):
    monkeypatch.setattr(
        hw, "get_repo_config", lambda i, r: {"guard_hygiene_watch_enabled": False})
    monkeypatch.setattr(
        hw, "_fetch_file",
        lambda t, o, r, p: (_ for _ in ()).throw(AssertionError("no fetch expected")),
    )
    assert hw.run_hygiene_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == (0, 0)


def test_lost_claim_writes_nothing(monkeypatch):
    writes = _wire(monkeypatch, claim=False)
    assert hw.run_hygiene_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}]) == (0, 0)
    assert writes == []


def test_one_bad_repo_does_not_abort_the_rest(monkeypatch):
    """Best-effort per repo: the poller must keep going for every other
    repo when one repo's GitHub call fails."""
    writes = _wire(monkeypatch)

    def _boom(t, o, r):
        if o == "bad":
            raise httpx.RequestError("boom", request=httpx.Request("GET", "https://x"))
        return [".github/workflows/ci.yml"]

    monkeypatch.setattr(hw, "_discover_files", _boom)
    filed, failed = hw.run_hygiene_watch_for_install(
        "tok", 1, [{"id": 1, "full_name": "bad/r"}, {"id": 2, "full_name": "o/r"}],
    )
    assert (filed, failed) == (1, 1)


def test_4xx_write_failure_releases_the_claim(monkeypatch):
    """The claim must represent a FILED report, not an attempt - a definite
    no-write releases so the next tick retries."""
    _wire(monkeypatch)
    released = []
    monkeypatch.setattr(
        hw, "release_hygiene_watch_report", lambda i, r: released.append((i, r)))
    monkeypatch.setattr(
        hw.httpx, "post",
        lambda url, **kw: (_ for _ in ()).throw(httpx.HTTPStatusError(
            "422", request=httpx.Request("POST", url),
            response=httpx.Response(422, request=httpx.Request("POST", url)))),
    )
    filed, failed = hw.run_hygiene_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert (filed, failed) == (0, 1)
    assert released == [(1, "o/r")]


def test_5xx_write_failure_keeps_the_claim(monkeypatch):
    """Ambiguous outcome - the write may have landed. Keeping the claim
    means a missed weekly report instead of a duplicate issue."""
    _wire(monkeypatch)
    released = []
    monkeypatch.setattr(
        hw, "release_hygiene_watch_report", lambda i, r: released.append((i, r)))
    monkeypatch.setattr(
        hw.httpx, "post",
        lambda url, **kw: (_ for _ in ()).throw(httpx.HTTPStatusError(
            "503", request=httpx.Request("POST", url),
            response=httpx.Response(503, request=httpx.Request("POST", url)))),
    )
    filed, failed = hw.run_hygiene_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert (filed, failed) == (0, 1)
    assert released == []


def test_report_rows_are_capped_and_the_cap_is_disclosed(monkeypatch):
    """No silent truncation: a capped report must say what it dropped."""
    many = "jobs:\n" + "".join(
        f"  j{i}:\n    runs-on: ubuntu-latest\n" for i in range(hw._MAX_REPORT_ROWS + 5)
    )
    writes = _wire(monkeypatch, content=many)
    hw.run_hygiene_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    body = writes[0][2]["body"]
    assert "and 5 more (capped)" in body


# --- #778: dead-ref patterns actually reach the scanner -------------------

def test_configured_dead_ref_pattern_is_found_end_to_end(monkeypatch):
    """#778 acceptance: with a pattern configured for the repo, a fixture
    file containing a genuine dead reference produces exactly one dead-ref
    violation through the FULL run loop (config read -> scan_file ->
    filed report) - not just a scan_dead_refs unit test.

    Before the fix, run_hygiene_watch_for_install called
    `scan_file(path, text)` with no patterns, so `dead_patterns` fell back
    to its `()` default and this file (otherwise hygiene-clean) produced
    ZERO violations - no report would be filed at all (filed == 0), which
    is exactly what this test guards against.
    """
    fixture = "\n".join([
        "jobs:",
        "  build:",
        "    runs-on: ubuntu-latest",
        "    timeout-minutes: 5",
        "    steps:",
        "      - run: echo deploying to /old/cluster/path",
    ])
    writes = _wire(monkeypatch, content=fixture, dead_ref_patterns=("/old/cluster/path",))
    filed, failed = hw.run_hygiene_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert (filed, failed) == (1, 0)
    body = writes[0][2]["body"]
    assert "`dead-ref`" in body
    assert "reference to decommissioned `/old/cluster/path`" in body
    assert "UNCONFIGURED" not in body


def test_unconfigured_dead_ref_category_is_logged_every_tick(monkeypatch, caplog):
    """#778 acceptance: while no patterns are configured, that state must
    be VISIBLE (log), so an empty dead-ref result is never mistaken for a
    clean scan - even on a repo that is otherwise fully clean and files no
    report at all (the only place the state would otherwise show up)."""
    clean = "jobs:\n  build:\n    runs-on: ubuntu-latest\n    timeout-minutes: 5\n"
    _wire(monkeypatch, content=clean)  # dead_ref_patterns=() by default
    with caplog.at_level("INFO", logger=hw.log.name):
        filed, failed = hw.run_hygiene_watch_for_install(
            "tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert (filed, failed) == (0, 0)
    assert any(
        r.message == "hygiene_watch_dead_ref_unconfigured" for r in caplog.records
    )


def test_unconfigured_dead_ref_category_is_disclosed_in_a_filed_report(monkeypatch):
    """#778 acceptance: when a report IS filed for other categories while
    dead-ref has no patterns, the report body must say the category is
    unconfigured - a reader of the weekly report (not just the logs) must
    not read the absent dead-ref rows as a clean scan for that category."""
    writes = _wire(monkeypatch)  # _DIRTY trips job-timeout; no patterns configured
    filed, failed = hw.run_hygiene_watch_for_install("tok", 1, [{"id": 9, "full_name": "o/r"}])
    assert (filed, failed) == (1, 0)
    body = writes[0][2]["body"]
    assert "job-timeout" in body
    assert "| `dead-ref` |" not in body  # no dead-ref finding row - it was never checked
    assert "UNCONFIGURED" in body
    assert "docs/SELF_HOST.md" in body
