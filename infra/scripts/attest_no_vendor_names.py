#!/usr/bin/env python3
"""Public-repo naming guard: competitor product names must never appear.

This repo is PUBLIC. Standing policy is that competitor products are
referenced by CODEWORD only, never by product name. A 2026-07-24 sweep
found the policy had been violated ~135 times across ~53 files - almost
all of them in-code finding attributions of the shape
`# <vendor> #685: <what it caught>`.

The ATTRIBUTION is correct and wanted (traceability for which reviewer
caught which class of bug). Only the NAME is wrong. So this guard does not
discourage attribution - it just makes the codeword the only spelling that
survives CI.

Deliberate carve-outs, each for a concrete reason:

  - BOT-AUTHORED content. Review bots inject their own HTML markers into
    PR bodies and their own comment payloads; test fixtures that replay
    those payloads must keep them byte-exact or they stop reproducing the
    thing they pin. A bot's identity is inherently public as a comment
    author anyway - it is not ours to launder.
  - `GRUG_HARVEST_SRC_A_LOGIN` / `SRC_B_LOGIN`. Reviewer logins are
    RUNTIME config, never committed, and the corpus labels are already
    neutral (`src-a` / `src-b`). Those env-var NAMES are correct as-is.
  - `code_review_prompt.py`. SHA-pinned byte-for-byte by the elder_eval
    baseline gate; a cosmetic comment edit there would invalidate a
    baseline that can only be re-recorded by running live models against
    the Sparks. Rename it opportunistically the next time a real prompt
    change re-records that baseline anyway.
  - `docs/research/`. Tracked separately - a competitive research brief
    is a narrative problem, not a find-and-replace one, and deleting docs
    is an operator call.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Product names that must not appear. Keep lowercase; matching is
# case-insensitive. Codewords live in the operator's private decoder, NOT
# here - this file only needs to know what is forbidden, never the mapping.
FORBIDDEN = (
    "coderabbit",
    "qodo",
    "greptile",
    "sourcery",
    "codium",
)

# Trees to scan. Anything not listed is out of scope for this guard.
SCAN_ROOTS = ("services", "web/src", "infra", "specs")
SCAN_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".yml", ".yaml", ".md", ".sh")

SKIP_PARTS = (
    "__pycache__",
    "node_modules",
    ".venv",
    "/dist/",
    "/build/",
)

# Path-level carve-outs (see module docstring for the reason on each).
EXEMPT_PATHS = (
    "services/_shared/code_review_prompt.py",
    "docs/research/",
    # This guard names the forbidden strings by necessity.
    "infra/scripts/attest_no_vendor_names.py",
)

# Line-level carve-outs: a line matching any of these is bot-authored or
# functional config rather than our own prose.
EXEMPT_LINE_PATTERNS = (
    re.compile(r"GRUG_HARVEST_SRC_[AB]_LOGIN"),
    # Bot-injected HTML markers replayed verbatim in fixtures.
    re.compile(r"<!--\s*(?:end of\s*)?auto-generated", re.I),
    re.compile(r"cr-comment:v\d|cr-indicator-types|fingerprinting:phantom"),
    # A bot's own login, used to MATCH its comments - renaming would break
    # the match. These belong in runtime config long-term (see #753).
    re.compile(r"\[bot\]"),
)

_FORBIDDEN_RE = re.compile("|".join(FORBIDDEN), re.I)


def _in_scope(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT).as_posix()
    if any(part in f"/{rel}" for part in SKIP_PARTS):
        return False
    if any(rel.startswith(x) or rel == x for x in EXEMPT_PATHS):
        return False
    return path.suffix in SCAN_SUFFIXES


def _candidate_files() -> list[Path]:
    out: list[Path] = []
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and _in_scope(path):
                out.append(path)
    return out


def main() -> int:
    findings: list[str] = []
    for path in _candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable - nothing to lint
        for line_no, line in enumerate(text.splitlines(), 1):
            if not _FORBIDDEN_RE.search(line):
                continue
            if any(p.search(line) for p in EXEMPT_LINE_PATTERNS):
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            findings.append(f"{rel}:{line_no}: {line.strip()[:100]}")

    if findings:
        print(
            "FAIL: competitor product name(s) found in this PUBLIC repo. Use the\n"
            "codeword instead - keep the attribution, change only the name.\n"
            "If this line is bot-authored or functional config, add it to\n"
            "EXEMPT_LINE_PATTERNS with a reason.\n",
            file=sys.stderr,
        )
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        return 1

    print(f"OK: no competitor product names in {len(_candidate_files())} scanned files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
