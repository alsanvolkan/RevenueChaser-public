---
name: rc-issue-tracking
description: Track RevenueChaser work on GitHub — open an issue for every slice, review finding or research question, put it on Projects board #1 with a Status, and attach it as a sub-issue of its area epic. Use whenever a review, plan or discussion produces an actionable item, when work on an issue starts or ships (Todo → In Progress → Done), or when the user asks to "open an issue", "add to the roadmap/board", or "track this".
---

# RevenueChaser issue tracking

The user's rule (2026-09-30): **everything is tracked as a GitHub issue, and every issue
hangs under an epic as a sub-issue.** Plan files in `.hermes/plans_claude/` stay as the
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
`.hermes/plans_claude/2026-09-30_plan_restructure.md` §5). Record each decision with
`--review`, as a comment on the issue, and as a packet summary on the area epic.
- Labels: `area:<key>` (exactly one per issue), `epic`, `research`, `bug`, `enhancement`,
  `idea`, `documentation`, `new dashboard`, `missing module`, `priority: high|medium|low`.

## Hierarchy (at most 3 levels)

`area epic` → `feature / plan epic` → `slice`.

| Area key | Area epic | Feature epics / notable children |
|---|---|---|
| `platform-store` | #49 | #21 |
| `platform-app` | #50 | #22 |
| `providers` | #51 | #23, #12 |
| `spine` | #52 | #35 GICS accuracy, #9 |
| `data-health` | #53 | #36 Settings v2 → #37–#41 |
| `shared-ui` | #54 | #32 |
| `classification` | #55 | #29, #31 research → #42, #43 · #33 Value Chain → #34, #61 |
| `screeners` | #56 | #27, #2, #3, #4, #13, #14 |
| `market` | #57 | #24, #26, #30, #5, #6, #7 |
| `earnings` | #58 | #28, #15 |
| `ownership` | #59 | #25 |
| `quality` | #60 | #11 |
| `community` | #79 | #72 Discord ingest + board → #73–#75, #77, #78 |

Cross-cutting: **#44 Project structure** → #45 A, #46 B, #47 C, #48 D (no area).
Pass `--area <key>` to the helper so the label and the board field are both set.

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
