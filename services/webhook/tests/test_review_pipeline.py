"""Behavior tests for Elder's bounded review planner."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from review_pipeline import ReviewCoverage, plan_review, render_review_map


@dataclass(frozen=True)
class _Hunk:
    path: str
    body: str


def test_small_diff_stays_on_single_review_path() -> None:
    plan = plan_review(
        [_Hunk("src/a.py", "a" * 20), _Hunk("tests/test_a.py", "b" * 20)],
        max_cohort_chars=100,
    )

    assert not plan.staged
    assert plan.cohorts[0].hunk_indexes == (0, 1)
    assert render_review_map(plan) == ""


def test_large_diff_keeps_matching_implementation_and_test_together() -> None:
    hunks = [
        _Hunk("src/a.py", "a" * 40),
        _Hunk("tests/test_a.py", "t" * 40),
        _Hunk("src/b.py", "b" * 40),
    ]

    plan = plan_review(hunks, max_cohort_chars=90)

    assert plan.staged
    assert plan.cohorts[0].hunk_indexes == (0, 1)
    assert plan.cohorts[0].layers == ("implementation", "verification")
    assert plan.cohorts[1].hunk_indexes == (2,)


def test_oversized_area_is_split_without_splitting_hunks() -> None:
    hunks = [
        _Hunk("src/a.py", "a" * 60),
        _Hunk("src/b.py", "b" * 60),
        _Hunk("src/c.py", "c" * 120),
    ]

    plan = plan_review(hunks, max_cohort_chars=100)

    assert [cohort.hunk_indexes for cohort in plan.cohorts] == [(0,), (1,), (2,)]
    assert plan.cohorts[-1].diff_chars == 120
    assert not plan.cohorts[0].oversized
    assert plan.cohorts[-1].oversized


def test_review_map_shares_structure_but_not_diff_content() -> None:
    plan = plan_review(
        [_Hunk("src/a.py", "SECRET-DIFF" * 5), _Hunk("docs/readme.md", "x" * 60)],
        max_cohort_chars=60,
    )

    rendered = render_review_map(plan)

    assert "Cohort 1" in rendered
    assert "src/a.py" in rendered
    assert "docs/readme.md" in rendered
    assert "SECRET-DIFF" not in rendered


def test_invalid_cohort_budget_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        plan_review([_Hunk("x.py", "x")], max_cohort_chars=0)


def test_many_small_files_are_bounded_by_path_count() -> None:
    hunks = [_Hunk(f"src/file_{index}.py", "x") for index in range(5)]

    plan = plan_review(hunks, max_cohort_chars=100, max_cohort_paths=2)

    assert [len(cohort.paths) for cohort in plan.cohorts] == [2, 2, 1]


def test_foundational_contracts_are_ordered_before_consumers_and_tests() -> None:
    plan = plan_review(
        [
            _Hunk("tests/test_user.py", "t" * 60),
            _Hunk("src/user_service.py", "i" * 60),
            _Hunk("schemas/user.py", "s" * 60),
        ],
        max_cohort_chars=70,
    )

    assert [cohort.label for cohort in plan.cohorts] == [
        "schemas",
        "src",
        "tests",
    ]
    assert [cohort.layers for cohort in plan.cohorts] == [
        ("contract",),
        ("implementation",),
        ("verification",),
    ]


def test_reviewability_reports_oversized_hunk_and_cross_cohort_module() -> None:
    plan = plan_review(
        [
            _Hunk("src/tangled.py", "a" * 120),
            _Hunk("src/tangled.py", "b" * 80),
        ],
        max_cohort_chars=100,
    )

    assert {concern.kind for concern in plan.concerns} == {
        "oversized-hunk",
        "cross-cohort-module",
    }
    cross = next(c for c in plan.concerns if c.kind == "cross-cohort-module")
    assert cross.paths == ("src/tangled.py",)


def test_reviewability_reports_semantic_proof_split_across_cohorts() -> None:
    plan = plan_review(
        [
            _Hunk("src/account.py", "a" * 60),
            _Hunk("tests/test_account.py", "t" * 60),
        ],
        max_cohort_chars=100,
    )

    concern = next(item for item in plan.concerns if item.kind == "cross-cohort-proof")
    assert concern.paths == ("src/account.py", "tests/test_account.py")
    assert "implementation and verification" in concern.message


def test_review_map_exposes_layers_and_reviewability_without_diff_content() -> None:
    plan = plan_review(
        [_Hunk("src/tangled.py", "SECRET" * 30)],
        max_cohort_chars=100,
    )

    rendered = render_review_map(plan)

    assert "layers: implementation" in rendered
    assert "Reviewability warning" in rendered
    assert "SECRET" not in rendered


# --- budget-aware planning ---------------------------------------------------
#
# The planner used to optimize only for SIZE while the executor was bound by
# TIME, and neither knew about the other. One PR planned 18 serial
# cohorts into a 700s budget whose 330s reserve leaves ~370s of real cohort
# time; cohorts 15-18 were never attempted and surfaced as "failed", which
# reads as an outage rather than "this PR is too big to review in full".


def _many_areas(count: int) -> list[_Hunk]:
    return [_Hunk(f"area{i}/impl.py", "x" * 60) for i in range(count)]


def test_plan_without_a_cohort_cap_is_unchanged() -> None:
    hunks = _many_areas(8)
    assert len(plan_review(hunks, max_cohort_chars=100).cohorts) == 8


def test_plan_is_truncated_to_the_runnable_cohort_count() -> None:
    plan = plan_review(_many_areas(8), max_cohort_chars=100, max_cohorts=3)

    assert len(plan.cohorts) == 3
    concern = next(item for item in plan.concerns if item.kind == "plan-truncated")
    # The unreviewed areas are NAMED, so the omission is honest rather than
    # arriving later as five mystery cohort failures.
    assert concern.paths == tuple(f"area{i}/impl.py" for i in range(3, 8))
    assert "5" in concern.message


def test_truncation_keeps_the_highest_value_prefix() -> None:
    """_ordered_areas already sorts contract -> implementation ->
    verification -> documentation, so the surviving prefix is the most
    load-bearing content, not an arbitrary slice."""
    hunks = [
        _Hunk("docs/guide.md", "d" * 60),
        _Hunk("src/impl.py", "i" * 60),
        _Hunk("schema/types.py", "s" * 60),
    ]

    plan = plan_review(hunks, max_cohort_chars=100, max_cohorts=1)

    assert plan.cohorts[0].layers == ("contract",)
    concern = next(item for item in plan.concerns if item.kind == "plan-truncated")
    assert "docs/guide.md" in concern.paths


def test_cap_at_or_above_the_plan_size_adds_no_concern() -> None:
    plan = plan_review(_many_areas(3), max_cohort_chars=100, max_cohorts=3)

    assert len(plan.cohorts) == 3
    assert not [item for item in plan.concerns if item.kind == "plan-truncated"]


def test_small_single_cohort_diff_ignores_the_cap() -> None:
    plan = plan_review(
        [_Hunk("src/a.py", "a" * 20)], max_cohort_chars=100, max_cohorts=1,
    )

    assert not plan.staged
    assert not [item for item in plan.concerns if item.kind == "plan-truncated"]


def test_non_positive_cohort_cap_is_rejected() -> None:
    # A misparsed config value must not silently reduce every review to
    # nothing - fail loudly like the other planner budgets do.
    with pytest.raises(ValueError):
        plan_review(_many_areas(4), max_cohort_chars=100, max_cohorts=0)


# --- #813/#707: truncated plans must say so in a NUMBER, not just prose ----


def test_truncated_plan_reports_the_true_planned_total() -> None:
    """`total_cohorts_planned` is what the packer WOULD have run - kept +
    dropped - not `len(cohorts)`. A caller deriving coverage only from
    `len(cohorts)` cannot tell "reviewed everything" from "reviewed
    everything it was allowed to start"."""
    plan = plan_review(_many_areas(8), max_cohort_chars=100, max_cohorts=3)

    assert len(plan.cohorts) == 3
    assert plan.total_cohorts_planned == 8
    assert plan.truncated is True


def test_untruncated_plan_total_matches_cohort_count() -> None:
    plan = plan_review(_many_areas(3), max_cohort_chars=100, max_cohorts=3)

    assert plan.total_cohorts_planned == len(plan.cohorts) == 3
    assert plan.truncated is False


def test_single_cohort_plan_is_never_truncated() -> None:
    plan = plan_review([_Hunk("src/a.py", "a" * 20)], max_cohort_chars=100)

    assert plan.total_cohorts_planned == 1
    assert plan.truncated is False


def test_empty_diff_plan_is_never_truncated() -> None:
    plan = plan_review([], max_cohort_chars=100)

    assert plan.cohorts == ()
    assert plan.total_cohorts_planned == 0
    assert plan.truncated is False


def test_coverage_fraction_is_a_number_not_a_mood() -> None:
    """(#645 eval harness) `fraction` is the one field a harness can chart
    over time without parsing board prose."""
    full = ReviewCoverage(
        total_cohorts=4, completed_cohorts=4, failed_cohorts=(),
        cohort_labels=("a", "b", "c", "d"),
    )
    assert full.fraction == 1.0
    assert full.complete is True

    half = ReviewCoverage(
        total_cohorts=4, completed_cohorts=2, failed_cohorts=(3, 4),
        cohort_labels=("a", "b", "c", "d"),
    )
    assert half.fraction == 0.5
    assert half.complete is False

    empty = ReviewCoverage(
        total_cohorts=0, completed_cohorts=0, failed_cohorts=(), cohort_labels=(),
    )
    assert empty.fraction == 1.0  # nothing planned covers all of nothing
