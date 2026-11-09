---
name: rc-batch-landing
description: Use when landing RevenueChaser work on main — how a branch becomes a merged commit: which items get their own PR and never ride with others, what a build thread hands over, the review and merge steps, the SELECTIVE/FULL merge gate, and the live-change gate. Load it before opening a PR or merging one.
---

# Landing RevenueChaser work on main

Adopted by avo on 2026-10-02 as a batching rule, because GitHub Actions minutes were scarce on the private
repository. **Development moved to the public repository and minutes are no longer scarce**, so the batching
default is gone: today the rule is *few pull requests, each carrying real work, one per lane*
([`CLAUDE.md`](../../../CLAUDE.md), "Where work happens"). Everything else below — what never shares a PR,
what a build thread hands over, how a merge is pinned — still holds, and is the reason this skill exists.

## One PR per lane

- One pull request per lane. A one-line follow-up rides the next PR that is open anyway rather than getting
  one of its own.
- The reason to put two slices in one PR is now **shared files**, not cost: two lanes that touch the same file
  are cheaper to review and merge together than to rebase past each other.
- A `batch/<YYYY-MM-DD>` collecting branch is no longer the default. It is still the right shape when several
  finished branches must land as one reviewable unit — then the body references every issue and every rollback.

## These always get their own PR, never batched

Each one carries a backup and a named rollback, and has to be revertable alone:

- a `_SCHEMA_VERSION` bump — it also needs a store checkpoint taken **before** the merge
  (`python -m RevenueChaser.rcdata backup`); there is no automation for it;
- any write to the live store;
- a change to a job the dashboard runs.

**The live-change gate**, for the third one: before the merge, a fresh verified backup, an idle `/api/jobs`,
and read-only before-counts; after the merge, pull, restart, one `GET /view/<module>?refresh=1`, and
after-counts. Any change to **what a board shows** goes to avo as before/after on real data first; he
approves.

## Build threads

1. Build only an issue that is approved to build. Every change maps to a GitHub issue of this repository,
   attached as a sub-issue of its area epic, carrying its `area:*` and `board:*` labels.
2. Branch `area/<key>/<slice>`, where `<key>` is the issue's `area:` label.
3. Work test-first, read in CI: "seen red first" is the `red-on-base` job's result, quoted in the report
   ("e2e: not checked" said plainly). A mutation check, only for money, store, identity and migration logic, is a
   pushed mutation commit read red, then a revert commit. "Not done" is an acceptable answer, an implied "done"
   is not.
4. **Run no test yourself**, not one file: CI runs the changed-module unit tests and the e2e tier.
5. Push your own branch, open a **draft** PR into `main` at the first push, and report the branch, the full head
   SHA, the `red-on-base` line and the mutation checks. **Build threads never merge.**

## CI, and the merge gate

- `.github/workflows/tests.yml` runs both tiers on every pull request whose base is `main`, and on `main`. A
  push to a work branch runs nothing until its PR to `main` exists.
- A push to a PR runs only the test files that import a changed file (`scripts/select_tests.py`; check names
  `unit (SELECTIVE)` / `e2e (SELECTIVE)`), and falls back to FULL when that is not safe to say. `main` and a
  manual dispatch are always FULL.
- **The merge gate is a green `merge-gate` check on the current head.** It fails on purpose for a SELECTIVE
  run. Label the PR `full-ci` before merging; the label stays, so every later push is FULL too.
- A docs-only change (and a change under `.claude/`) is filtered out and leaves **no** check on the PR. That
  is a skipped run, not a missing one.
- **Never re-run CI to get past a failure.** A failure the diff cannot explain is a flaky test: fix it, or
  remove its host assumption, in the same branch.

## Merge

1. Review first: read `git diff --stat` and the test results. Under 200 changed lines and green, a careful
   self-review is enough; 200+ lines, critical logic (money, store, data, identity), security, or any red test
   gets a separate review thread given the diff and the test result, not your conclusion.
2. Check staleness: did `main` move? If the changed files intersect, update the branch first.
3. Merge with a merge commit **pinned to the reviewed head SHA** (`sha=<head>` in the GitHub merge API), after
   green CI and the review.
4. Record the merge on the issue. Close any superseded per-item PR as "landed via <this PR>".
5. After the merge: fresh backup, idle `/api/jobs`, `git pull --ff-only` in the live checkout, restart, one
   Refresh of a touched board, verify. **"Done" means merged AND verified running.**

## Never

- A build thread never merges, and never asks another thread to merge on its behalf.
- Never re-run CI to get past a failure: fix it or drop the item.
- No empty commit, and no close-and-reopen, to trigger CI.
- Never rewrite history on someone else's branch.
