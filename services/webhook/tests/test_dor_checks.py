"""Tests for static DoR checks.

Critical regression: closes #20 — empty `- [ ]` placeholders must NOT
count as filled bullets (security: unfilled template should NOT pass).
"""

from __future__ import annotations

from personas.tpm.dor_checks import (
    check_acceptance,
    check_estimate,
    check_issue_link,
    check_linked_issue_completeness,
    check_scope_fence,
    check_why,
    run_all,
)


def test_why_passes_with_5_words():
    body = "## Why\nWe need this for the launch tomorrow morning"
    assert check_why(body).passed


def test_why_fails_under_5_words():
    body = "## Why\ntoo short"
    r = check_why(body)
    assert not r.passed and "2 words" in r.detail


def test_why_missing_section():
    assert not check_why("nothing").passed


def test_why_falls_back_to_summary():
    body = "## Summary\nthis is a longer summary line"
    assert check_why(body).passed


def test_acceptance_three_filled_bullets_passes():
    body = "## Acceptance criteria\n- [x] one\n- [x] two\n- [x] three"
    assert check_acceptance(body).passed


def test_acceptance_empty_placeholders_reject_closes_20():
    """The bug from #20: `- [ ]` empty checkboxes must not count."""
    body = "## Acceptance criteria\n- [ ]\n- [ ]\n- [ ]"
    assert not check_acceptance(body).passed


def test_acceptance_mixed_empty_and_filled():
    body = "## Acceptance criteria\n- [x] real\n- [ ]\n- [ ]"
    r = check_acceptance(body)
    assert not r.passed and "1 non-empty" in r.detail


def test_acceptance_falls_back_to_test_plan():
    body = "## Test plan\n- a\n- b\n- c"
    assert check_acceptance(body).passed


# external-review P2 on PR #40 — error msg must reference the section the user
# actually used, not always say "Acceptance criteria".
def test_acceptance_error_msg_says_test_plan_when_thats_what_user_used():
    body = "## Test plan\n- only one"
    r = check_acceptance(body)
    assert not r.passed
    assert "Test plan" in r.detail
    assert "Acceptance criteria has" not in r.detail


def test_acceptance_error_msg_says_acceptance_criteria_when_used():
    body = "## Acceptance criteria\n- only one"
    r = check_acceptance(body)
    assert not r.passed
    assert "Acceptance criteria" in r.detail


def test_estimate_pass():
    assert check_estimate("Size: M somewhere in body").passed


def test_estimate_xl_fails():
    r = check_estimate("Size: XL")
    assert not r.passed and "split" in r.detail.lower()


def test_estimate_missing():
    assert not check_estimate("no size here").passed


# Seer MED on PR #40 — _SIZE_PAT must require Size: prefix, NOT match
# bare letters in random prose.
def test_estimate_rejects_bare_letter_in_prose():
    """`M&Ms` / `the M key` / `XL t-shirts` must NOT satisfy estimate."""
    for body in [
        "use the M key",
        "lots of M&Ms",
        "XL t-shirts on sale",
        "sentence with L in it",
        "Size is fine but no value supplied",  # `Size` alone w/o letter
    ]:
        assert not check_estimate(body).passed, f"falsely accepted: {body!r}"


def test_estimate_accepts_explicit_size_prefix_variants():
    for body in [
        "Size: M",
        "Size:M",
        "Size M",
        "**Size:** S",
        "size: l",  # lowercase
    ]:
        assert check_estimate(body).passed, f"should accept: {body!r}"


def test_scope_fence_pass():
    assert check_scope_fence("## Out of scope\nstuff").passed


def test_scope_fence_missing():
    assert not check_scope_fence("nothing").passed


def test_issue_link_variants():
    for kw in ["closes", "Fixes", "Resolves", "Part of"]:
        assert check_issue_link(f"{kw} #42").passed


def test_issue_link_missing():
    assert not check_issue_link("just text").passed


def test_run_all_returns_6():
    results = run_all("")
    assert len(results) == 6
    assert {r.name for r in results} == {
        "why", "acceptance", "estimate", "scope-fence", "issue-link",
        "linked-issue-completeness",
    }


# --- Tolerant heading matching (qualifier suffix after the canonical name) ---

def test_scope_fence_tolerates_parenthetical_suffix():
    # `## Out of scope (→ #828 go-live)` should satisfy scope-fence — the
    # brittle exact-match used to fail this otherwise-correct header.
    assert check_scope_fence("## Out of scope (→ #828 go-live)\n\nDeferred.").passed


def test_scope_fence_tolerates_dash_suffix():
    assert check_scope_fence("## Out of scope — later\n\nNot now.").passed


def test_why_tolerates_trailing_words():
    body = "## Why this matters\n\nbecause the bill must trend toward zero over time"
    assert check_why(body).passed


def test_acceptance_tolerates_test_plan_qualifier():
    body = "## Test plan (manual)\n\n- [ ] a\n- [ ] b\n- [ ] c\n"
    assert check_acceptance(body).passed


def test_heading_suffix_requires_word_boundary_no_false_match():
    # `## Summarytext` must NOT match `Summary` (no boundary char after the
    # name) — guards against the prefix match being too loose.
    assert not check_why("## Summarytext\n\nplenty of words here to clear five").passed
    # but a real `## Summary ...` still works
    assert check_why("## Summary of the change\n\nfive or more words present here").passed


# --- check_linked_issue_completeness (#564) ---


def test_linked_issue_no_closing_keyword_passes():
    """PR body with no closing keyword -> pass (N/A, nothing to check)."""
    body = "## Why\nthis is a real why\n## Acceptance criteria\n- [x] a\n- [x] b\n- [x] c\ncloses #1\n## Out of scope\nnothing\n**Size:** M"
    # Remove the closing keyword to test the no-match path.
    body_no_close = body.replace("closes #1\n", "")
    r = check_linked_issue_completeness(body_no_close)
    assert r.passed
    assert "no linked issues" in r.detail


def test_linked_issue_all_ticked_passes():
    """One linked issue, all checkboxes ticked -> pass."""
    body = "closes #42\n**Size:** M"
    def fetcher(_num: int) -> str:
        return "## Acceptance\n- [x] done one\n- [x] done two\n"
    r = check_linked_issue_completeness(body, fetch_issue=fetcher)
    assert r.passed
    assert "all checkboxes ticked" in r.detail


def test_linked_issue_unchecked_nonexempt_fails():
    """One linked issue with an unchecked non-exempt box -> fail, names issue+item."""
    body = "closes #42\n**Size:** M"
    def fetcher(_num: int) -> str:
        return "## Acceptance\n- [x] done one\n- [ ] still open\n"
    r = check_linked_issue_completeness(body, fetch_issue=fetcher)
    assert not r.passed
    assert "#42" in r.detail
    assert "still open" in r.detail


def test_linked_issue_unchecked_under_exempt_heading_passes():
    """An unchecked box under an exempt heading (Out of scope) -> pass."""
    body = "closes #42\n**Size:** M"
    def fetcher(_num: int) -> str:
        return "## Out of scope\n- [ ] deferred item\n"
    r = check_linked_issue_completeness(body, fetch_issue=fetcher)
    assert r.passed
    assert "all checkboxes ticked" in r.detail


def test_linked_issue_multiple_only_one_has_gap_fails():
    """Multiple linked issues, only one has a gap -> fail, names the right one."""
    body = "closes #10 closes #20\n**Size:** M"
    def fetcher(num: int) -> str:
        if num == 10:
            return "## Acceptance\n- [x] done\n- [x] done2\n"
        return "## Acceptance\n- [x] done\n- [ ] missing\n"
    r = check_linked_issue_completeness(body, fetch_issue=fetcher)
    assert not r.passed
    assert "#20" in r.detail
    assert "#10" not in r.detail.split("#20")[0]  # #10 should not appear before #20
    assert "missing" in r.detail


def test_linked_issue_fetch_failure_fails_open():
    """Fetch failure (exception) must fail OPEN, not block merge."""
    body = "closes #42\n**Size:** M"
    def fetcher(_num: int) -> str:
        raise RuntimeError("network blip")
    r = check_linked_issue_completeness(body, fetch_issue=fetcher)
    assert r.passed
    assert "fail-open" in r.detail


def test_linked_issue_no_fetcher_fails_open():
    """No fetcher provided at all -> fail open."""
    body = "closes #42\n**Size:** M"
    r = check_linked_issue_completeness(body)
    assert r.passed
    assert "fail-open" in r.detail
