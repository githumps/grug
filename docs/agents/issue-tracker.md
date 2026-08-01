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
  - Category/other: `arch-review`, `bug`, `dependencies`, `documentation`, `duplicate`, `enhancement`, `feature`, `gh-app-best-practice`, `good first issue`, `grug-pulse`, `help wanted`, `invalid`, `javascript`, `likely-resolved`, `nightly-bot`, `orphan-ok`, `prd`, `preview`, `question`, `stale`.
  - Live epic groups: `epic-arch-hygiene`, `epic-deploy-reliability`, `epic-enforcement`, `epic-resiliency`.
  - Retired (history only, never apply): `archived-epic-security` (complete 28/28), `archived-epic-grug-saas` (complete 40/40). Renamed rather than deleted on 2026-08-01 so the association survives on the 68 closed issues that carry them.

## Epic-link filing rule

**Every new issue carries an epic link or the `orphan-ok` label.** Put `Refs #N` in `## Dependencies` naming a live epic, or apply `orphan-ok` to declare the item deliberately standalone. An issue with neither is an orphan.

Why this exists: the backlog went from roughly 16 open issues on 2026-07-01 to 85 on 2026-07-31, while 34 of those 85 (40%) belonged to no epic. Intake ran at 159 issues per 30 days against 90 closed - the backlog grew regardless of throughput. Filing is frictionless and linking is not, so without a rule the orphan pool grows with the backlog and the epic list stops describing live work.

Live epics are issues with `epic` in the title; enumerate them rather than trusting this list:

```bash
gh issue list --repo quadseven/grug --state open --search "epic in:title" \
  --json number,title --jq '.[] | "#\(.number) \(.title)"'
```

`orphan-ok` is a real answer, not a loophole - a one-off ops fix or a chore genuinely belongs to no epic. It is a deliberate declaration, which is the point: the label makes the choice visible instead of silent.
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
