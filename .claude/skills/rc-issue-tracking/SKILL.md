---
name: rc-issue-tracking
description: Track RevenueChaser work on GitHub — open an issue for every slice, review finding or research question, put it on Projects board #1 with a Status, and attach it as a sub-issue of its area epic. Use whenever a review, plan or discussion produces an actionable item, when work on an issue starts or ships (Todo → In Progress → Done), or when the user asks to "open an issue", "add to the roadmap/board", or "track this".
---

# RevenueChaser issue tracking

The user's rule (2026-09-30): **everything is tracked as a GitHub issue, and every issue
hangs under an epic as a sub-issue.** Plan files (`plans_claude/`, in the private repo, not carried
here) stay as the
detailed work order and must cite their issue numbers. The GitHub board is the single
source of truth for status; never keep a local backlog file.

- Repo: `alsanvolkan/RevenueChaser-public` · Board: Projects v2 **#1** (owner `alsanvolkan`)
- Board fields: **Status** (`Todo` → `In Progress` → `Done`; moving an item to Done
  usually auto-closes the issue; check it and close it by hand if not), **Area** (one of
  the 13 keys below) and **Review** (next section).

## Review gate (user rule, 2026-09-30)

The user reviews every plan. **An issue is unreviewed until the user says so**, whoever
wrote it; never treat an issue body as a user decision. Review values: `Not reviewed`
(default) → `Reviewed` → `Approved to build` (**the only state that may go In
Progress**) → `Accepted` (built; the user has seen the evidence).

Levels:
- **Full**: anything that changes how a dashboard works (rules, data basis, filters,
  Fetch/Refresh, UI). Needs a cross-board impact list and a test plan before approval,
  and e2e plus a before/after on real data after the build.
- **Light**: invisible internals.
- **Research**: measurements only.

Review happens **one area at a time** via a review packet (see
`plans_claude/2026-09-30_plan_restructure.md` §5, in the private repo, not carried here). Record each decision with
`--review`, as a comment on the issue, and as a packet summary on the area epic.
- Labels: `area:<key>` (exactly one per issue), `epic`, `research`, `bug`, `enhancement`,
  `idea`, `documentation`, `new dashboard`, `missing module`, `priority: high|medium|low`.

## Hierarchy (at most 3 levels)

`epic` → `feature / plan epic` → `slice`.

The epics are per **board**, not per area key, so several share a key. Each row below is an open issue
in this repo carrying the `epic` label; read it from the repo rather than trusting this copy, because
issue numbers are the one thing a table like this gets wrong:

    gh issue list -R alsanvolkan/RevenueChaser-public --label epic --state open

| Epic | Board / scope | `--area` key |
|---|---|---|
| #34 | Inputs: every dashboard data request (D01–D37) | `providers` |
| #35 | Earnings Radar | `earnings` |
| #38 | SPX Gamma | `market` |
| #39 | LEAPS | `ownership` |
| #40 | Co-movement | `classification` |
| #41 | Asymmetric Growth | `screeners` |
| #42 | Settings / Data health | `data-health` |
| #43 | Common: shared work found in the dashboard reviews | `shared-ui` |
| #44 | Swing Potential | `screeners` |
| #45 | Financials | `market` |
| #46 | Fundamentals | `screeners` |
| #47 | Value Chain | `classification` |
| #48 | Relatedness (parked) | `classification` |
| #49 | Final pass: core + health ledger | `platform-app`, `platform-store`, `quality` |
| #56 | Sector Rotation: redevelopment, umbrella (parked) | `market` |
| #70 | Discord (parked) | `community` |

Pass `--area <key>` to the helper so the label and the board field are both set.

**Two gaps to know about.** Board #1's Area field offers 12 options and has no `community`, so
`--area community` creates and labels the issue and then exits on `unknown Area 'community'`, leaving
the Area field unset — use `--label area:community` for Discord work, or add the board option first.
And `spine` is a board Area option with no epic, so spine work has no parent to attach to.

**Big umbrella issues #22–#30** hold findings as checklists. **Convert an item to a
sub-issue only when work on it starts** (user decision), not in bulk.

## How

Use the helper next to this file. It creates the issue, adds it to the board, sets the
status and attaches the parent in one step:

```bash
py -3 .claude/skills/rc-issue-tracking/rc_issue.py --title "Settings Slice 4: ..." \
   --body-file body.md --label enhancement --label "priority: medium" --parent 36 --area data-health
py -3 .claude/skills/rc-issue-tracking/rc_issue.py --move 37 --status "In Progress"   # only if Review = Approved to build
py -3 .claude/skills/rc-issue-tracking/rc_issue.py --move 37 --review "Approved to build"
py -3 .claude/skills/rc-issue-tracking/rc_issue.py --link 35 --parent 44
```

Write the body file in the scratchpad, not the repo. Run with `PYTHONIOENCODING=utf-8`
when the text has non-ASCII characters.

## What a good issue body has

1. **Where the plan is:** plan path + section, and the review path if any.
2. **Evidence:** `path:line` and measured numbers (with the date). Say whether each claim
   is *verified* or *inferred*.
3. **Checklist** of concrete changes: files, functions, flags.
4. **Tests:** unit tests next to the code (seen red first), and e2e with a **mutation
   check** for anything touching a board. The full unit and e2e suites run in CI on the PR (see `docs/testing_framework.md`,
   `docs/e2e_testing.md`).
5. **Cross-board impact:** every other board, table or shared file this touches, and
   how. Example: `swing_ohlc` is read by both Swing and Co-movement, so a change to its
   price basis affects both. Write "none" only after checking the readers
   (`grep` the table and module names).
6. **Review level:** Full / Light / Research (see the Review gate above).
7. **Warnings:** schema bump (manual `.store/` checkpoint), protected area (build only
   after an explicit go, AGENTS.md §5 rule 4), network or credential needs.

Write bodies in English, like the existing issues. Talk to the user in their language.

## Lifecycle

- Start work → `--move N --status "In Progress"` and tick the plan slice.
- Ship → `Done`, close the issue with the commit/PR reference, log it in the plan's
  implementation log, and fold lasting outcomes into `docs/`.
- A review finds something new → a new issue under the matching epic. Do not append it to
  an old issue's body.
- Publishing to GitHub is outward-facing: issue creation that the user asked for is fine,
  but confirm before closing, deleting or bulk-editing issues they did not mention.
