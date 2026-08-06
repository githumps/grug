# Issue tracker: GitHub

Issues and PRDs for this repo live as GitHub issues at `quadseven/grug`. Use the `gh` CLI for all operations.

## Conventions

- **Title prefix:** `feat(grug): ` — the enforced default from `.github/ISSUE_TEMPLATE/work_item.md`. Older issues titled `[grug] ...` predate that template; new issues should use `feat(grug): `.
- **Body shape (from the work-item template, mirrors Grug's own DoR checker `Grug - Chief`):**
  - `## Why` — one paragraph, why this work exists.
  - `## Acceptance criteria` — >=3 checkbox bullets, each independently verifiable.
  - `## Size` — `**Size:** XS|S|M|L|XL` (`XS` <=30min, `S` <=2h, `M` <=1d, `L` <=3d; split `XL` into sub-issues first).
  - `## Dependencies` — `Blocked by #N`, `Refs #N`, or `closes #N` (plain text, not markdown-linked) so Grug's issue-link check passes. **Must carry an epic link** - see "Epic-link filing rule" below.
  - `## Out of scope` — adjacent work intentionally not covered.
  - The implementing PR mirrors this shape and must carry a plain-text `closes #<n>` for Grug's `Grug - Chief` DoR gate to pass.
- **Labels (full live set, verify with `gh label list --repo quadseven/grug` before relying on this list — it drifts):**
  - State-role: `needs-triage`, `ready-for-agent`, `wontfix`. The canonical five-role vocabulary's other two roles (`needs-info`, `ready-for-human`) are not yet defined as labels in this repo.
  - Category/other: `arch-review`, `bug`, `dependencies`, `documentation`, `duplicate`, `enhancement`, `feature`, `gh-app-best-practice`, `good first issue`, `grug-pulse`, `help wanted`, `invalid`, `javascript`, `likely-resolved`, `nightly-bot`, `prd`, `preview`, `question`, `stale`.
  - Live epic groups: `epic-arch-hygiene` (umbrella #823), `epic-deploy-reliability` (umbrella #824). Both now have a real epic ISSUE; the label is a filter, the sub-issue link is the membership.
  - Retired (history only, never apply): `archived-epic-security` (28/28), `archived-epic-grug-saas` (40/40), `archived-epic-resiliency` (5/5), `archived-epic-enforcement` (11/11). Renamed rather than deleted so the association survives on the closed issues that carry them.
  - Deleted: `orphan-ok` (2026-08-06) - see the filing rule below.

## Epic-link filing rule

**Every new issue is a sub-issue of an epic. There is no exception.**

Membership is a native GitHub **sub-issue** link, not a label and not a text reference. File the issue, then set its parent (Issues UI: "Add sub-issue" on the epic, or `POST /repos/quadseven/grug/issues/<epic>/sub_issues`).

Keep `Part of #N` in `## Dependencies` as well - it is what a reader sees in the body, and it is the tie-breaker when tooling has to infer. `Part of` beats `Refs`: `Refs` means "related", and treating it as parentage put three issues under two epics at once.

Why this exists: the backlog went from roughly 16 open issues on 2026-07-01 to 85 on 2026-07-31, while 34 of those 85 (40%) belonged to no epic. Intake ran at 159 issues per 30 days against 90 closed - the backlog grew regardless of throughput. Filing is frictionless and linking is not, so without a rule the orphan pool grows with the backlog and the epic list stops describing live work.

Live epics are issues with `epic` in the title; enumerate them rather than trusting this list:

```bash
gh issue list --repo quadseven/grug --state open --search "epic in:title" \
  --json number,title --jq '.[] | "#\(.number) \(.title)"'
```

The `orphan-ok` label is RETIRED (deleted 2026-08-06). It was the escape hatch in the first version of this rule; in practice it became the place work went to be forgotten. If something genuinely has no home, that is a signal the epic set is wrong - add an umbrella epic rather than an exception. Two were added this way: #823 (architecture hygiene) and #824 (deploy-pipeline reliability), both replacing bare `epic-*` labels that had no issue behind them.

Epic ROOTS have no parent, by definition: the epic issues themselves, and PRD umbrellas like #346. Everything else does.
- **Create an issue**: `gh issue create --repo quadseven/grug --title "feat(grug): ..." --body "..."`. Use a heredoc for multi-line bodies; follow the template's five-section shape above.
- **Read an issue**: `gh issue view <number> --repo quadseven/grug --comments`.
- **List issues**: `gh issue list --repo quadseven/grug --state open --json number,title,body,labels,comments`.
- **Comment on an issue**: `gh issue comment <number> --repo quadseven/grug --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --repo quadseven/grug --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --repo quadseven/grug --comment "..."`

Infer the repo from `git remote -v` when run inside a clone — `gh` does this automatically and the `--repo` flag above is only needed from outside one.

## Pull requests as a triage surface

**PRs as a request surface: no.** Set to `yes` if this repo should treat external PRs as feature requests; `/triage` reads this flag. Flip it here if that changes.

## When a skill says "publish to the issue tracker"

Create a GitHub issue: `gh issue create --repo quadseven/grug`.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --repo quadseven/grug --comments`.
