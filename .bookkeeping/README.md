# .bookkeeping: the data-system record

This folder is the plain record of how RevenueChaser's data works: where each dataset comes from, which code
writes it, where it is stored, how it is stored and how long it stays. It also tracks the data study (phases 1-4)
as its decisions land. The code is the source of truth; every value here cites a `path:line` at the commit named
in `tools/flow_data.py` (`AS_OF`).

| File | What it is |
|---|---|
| [data_system.md](data_system.md) | How the data system works, in prose: layers, store files, jobs, write rules, retention, backups, source links. |
| [data_flow.md](data_flow.md) | One row per dataset: source, writer function, file, table, key, write rule, how long it stays, and the planned target. Generated. |
| [data_flow.svg](data_flow.svg) | The overview chart: sources, then the jobs that write, then where it is stored. Generated. |
| [tools/flow_data.py](tools/flow_data.py) | The data behind both generated files. Edit this, not the generated files. |
| [tools/render_flow.py](tools/render_flow.py) | Regenerates `data_flow.md` and `data_flow.svg`; `--html PATH` also writes the browsable page. |

Related docs that stay authoritative for their own subject: [docs/data_architecture.md](../docs/data_architecture.md)
(storage layout, stages, schema evolution, backups) and [docs/data_retrieval.md](../docs/data_retrieval.md) (Fetch vs
Refresh).

## Keeping it current
- **When:** any change that adds, removes or renames a store table, changes a writer, a source, a key, a write rule,
  a schedule or a retention rule updates this folder in the same branch. So does each data-study decision (phase 2
  decisions D1-D8 and later ones) and each phase milestone.
- **How:** edit `tools/flow_data.py` (and `data_system.md` for prose), run
  `python .bookkeeping/tools/render_flow.py` from the repo root, and commit the regenerated `data_flow.md` and
  `data_flow.svg` with the change. Set `AS_OF` to the commit the values were read from.
- **Where else it shows:** the same page is published as the "RevenueChaser Data Flow" artifact; the plan is to
  show this record inside the dashboard under Settings / Data Health (the plan is in the private notes bookkeeping/data_system-decision-log.md).

