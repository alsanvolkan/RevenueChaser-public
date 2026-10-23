# Backlog & Roadmap — GitHub Projects board (AI source of truth)

Product ideas, backlog, and roadmap live on a **GitHub Projects v2 board**, not in
this repo. AI agents (and humans) should read **and update** that board, never a
stale local file. The former `future_ideas/` docs were migrated there.

## The board

- **Owner / account:** `alsanvolkan` (personal)
- **Project:** "Revenue Chaser" — number **1**
- **URL:** https://github.com/users/alsanvolkan/projects/1
- **Statuses (single-select):** `Todo` · `In Progress` · `Done`
- **Review (single-select):** `Not reviewed` · `Reviewed` · `Approved to build` · `Accepted`.
  Only `Approved to build` may go `In Progress`.

Requires the `gh` CLI authenticated to the `alsanvolkan` account with the
`project` scope (`gh auth status` → scopes must include `project`/`read:project`).

## Read the board (always do this first)

```bash
gh project item-list 1 --owner alsanvolkan          # items + titles + type
gh project view 1 --owner alsanvolkan               # board summary + item count
gh project field-list 1 --owner alsanvolkan         # fields/options (status ids)
```

The board is the single source of truth for what is planned, in progress, or
done. Check it before starting work, when reporting completion, and whenever a
"future idea" or roadmap question comes up.

## Status lifecycle

- New idea → **Todo**.
- Actively being built/worked → **In Progress**.
- Shipped/closed → **Done**.

`gh` has no CLI flag for the Status single-select, so move status with the
GraphQL mutation below. Field and option ids are stable per project (resolve via
`field-list` if they ever change).

```bash
gh api graphql -f query='
  mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
    updateProjectV2ItemFieldValue(input:{
      projectId:$projectId, itemId:$itemId, fieldId:$fieldId,
      value:{ singleSelectOptionId:$optionId }
    }){ projectV2Item { id } }
  }' \
  -f projectId='<project id>' \
  -f itemId='<item id>' \
  -f fieldId='<Status field id>' \
  -f optionId='<option id>'
```

Helper to get the ids (add `select(.name=="Status")` on the nodes array):

```bash
gh api graphql -f query='
  query($projectId: ID!) {
    node(id:$projectId){ ... on ProjectV2 {
      fields(first:50){ nodes {
        ... on ProjectV2SingleSelectField { name id options { id name } }
      } }
    } }
  }' -f projectId='<project id>'
```

## Add an item

- **From an issue** (keeps full text, comments, PR links in the repo):

  ```bash
  gh issue create -R alsanvolkan/RevenueChaser-public --title "…" --body "…"
  gh project item-add 1 --owner alsanvolkan --url <issue-url>
  ```

- **Standalone draft** (kanban only, no repo issue) — GraphQL, two steps:

  ```bash
  # 1) create the draft
  gh api graphql -f query='
    mutation($projectId: ID!, $title: String!) {
      addProjectV2DraftIssue(input:{ projectId:$projectId, title:$title }){
        projectItem{ id }
      }
    }' -f projectId='<project id>' -f title='…'
  # 2) set body (optional) and Status — body via updateProjectV2DraftIssue
  #    (needs the DraftIssue node id, resolve it from the item id first).
  ```

## Current items (as of the 2026-09 migration from future_ideas/)

- Sector/theme-specific screener rules (Issue #2)
- Biotech screener — Roadmap (4-phase, TNYA baseline)
- Long-Term Ranking Board (3 clocks, no full-universe download)
- Stock GEX / Greek Predictor (per-ticker)
- Weekly US Credit Market Report module
- Cross-Asset Derivatives Stress Module
- Forward-Testing: post-earnings drift (1w/2w) as a rule
- Russell 2000 membership (not yet implemented; deferred options)

Prefer the live board over this list — the board is authoritative.
