"""One review comment per PR, assembled from per-persona sections.

THE PROBLEM THIS SOLVES. Every persona posts its OWN top-level comment, so
a single PR produced three of them - measured on macchina#2115:

    03:38  <!-- grug-teller:walkthrough -->
    03:43  <!-- grug-elder-stack -->
    11:58  <!-- grug-sentinel:abandoned-review -->

GitHub emails on each, hours apart, so the author gets three notifications
for one review and has to reassemble the picture from an inbox.

THE LEVER. GitHub notifies on comment CREATION, not on EDIT. A single
comment that every persona EDITS therefore costs exactly ONE email no
matter how many personas contribute, and later passes refresh it silently.
That is the whole reason this module exists.

HOW IT WORKS. The board body carries one delimited region per persona:

    <!-- grug-board -->
    ...header...
    <!-- grug-sec:elder -->
    <details>...Elder's markdown...</details>
    <!-- /grug-sec:elder -->

`upsert_section` replaces exactly one region and leaves every other byte
untouched, so two personas writing concurrently can only clobber each
other's section, never each other's content. Sections render in a FIXED
order (`SECTION_ORDER`) regardless of arrival order, so the comment does
not reshuffle itself between passes - a board whose layout moves is one
nobody learns to skim.

Pure string in, pure string out: no HTTP, no store, no clock. The dispatch
layer owns fetching the existing body and PATCHing the new one, which is
what makes every rule here unit-testable without a network.
"""

from __future__ import annotations

import re

BOARD_MARKER = "<!-- grug-board -->"

# Fixed render order. A persona missing from a given pass simply has no
# region; it does not shift the others.
SECTION_ORDER: tuple[str, ...] = (
    "chief",      # DoR / ticket readiness - gate first
    "teller",     # walkthrough - what changed
    "elder",      # the review itself
    "guard",      # security / dependency
    "smasher",    # execution-class findings
    "sentinel",   # safety-net warnings - last, loudest
)

_OPEN = "<!-- grug-sec:{key} -->"
_CLOSE = "<!-- /grug-sec:{key} -->"


def _region(key: str) -> re.Pattern[str]:
    """Matches one persona's whole region INCLUDING both delimiters.

    `re.escape` on the delimiters, and a non-greedy body, so a section whose
    markdown happens to contain another section's marker text cannot make
    the match run past its own close tag.
    """
    return re.compile(
        re.escape(_OPEN.format(key=key)) + r".*?" + re.escape(_CLOSE.format(key=key)),
        re.DOTALL,
    )


def render_section(key: str, markdown: str) -> str:
    """One delimited region. Delimiters always on their own lines so a
    section ending in a fenced block cannot swallow the close tag."""
    return (
        f"{_OPEN.format(key=key)}\n{markdown.rstrip()}\n{_CLOSE.format(key=key)}"
    )


def section_keys(body: str) -> list[str]:
    """Persona keys currently present, in board order. Unknown keys (an
    older grug wrote a section this version does not know about) are kept
    and reported last rather than dropped - losing a section on upgrade
    would silently delete a persona's output."""
    found = set(re.findall(r"<!-- grug-sec:([a-z0-9_-]+) -->", body))
    known = [k for k in SECTION_ORDER if k in found]
    return known + sorted(found - set(SECTION_ORDER))


def upsert_section(body: str, key: str, markdown: str) -> str:
    """Return `body` with `key`'s region replaced, or inserted in order.

    Replacing in place is what keeps this concurrency-safe at the section
    level: a persona rewrites only the bytes between its own delimiters.
    """
    section = render_section(key, markdown)
    pat = _region(key)
    if pat.search(body):
        return pat.sub(lambda _m: section, body, count=1)

    # New section: insert so the result is in SECTION_ORDER, rather than
    # appending. Append order would depend on which persona happened to
    # finish first, so the board would look different on every PR.
    existing = section_keys(body)
    order = {k: i for i, k in enumerate(SECTION_ORDER)}
    after = [k for k in existing if order.get(k, 10_000) > order.get(key, 10_000)]
    if not after:
        return body.rstrip() + "\n\n" + section + "\n"
    first_after = after[0]
    anchor = _OPEN.format(key=first_after)
    idx = body.index(anchor)
    return body[:idx] + section + "\n\n" + body[idx:]


def new_board(header: str = "") -> str:
    """An empty board. The marker is FIRST so the comment-finder can match
    on it without parsing anything else."""
    head = f"\n{header.rstrip()}\n" if header.strip() else "\n"
    return f"{BOARD_MARKER}{head}"


def is_board(body: str) -> bool:
    return BOARD_MARKER in (body or "")


def collapse(summary: str, markdown: str, *, open_by_default: bool = False) -> str:
    """A GitHub-collapsible block.

    The blank lines around the body are REQUIRED: without them GitHub does
    not render markdown inside <details>, and the section silently degrades
    to literal asterisks and pipe characters. That is the single most common
    way a folded section looks broken.
    """
    attr = " open" if open_by_default else ""
    return (
        f"<details{attr}>\n<summary>{summary}</summary>\n\n"
        f"{markdown.strip()}\n\n</details>"
    )
