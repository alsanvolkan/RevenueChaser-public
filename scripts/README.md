# Raw-data migration

Move the legacy `rawData/` files + SQLite DBs into one DuckDB store:
`.store/raw.duckdb` (durable — never delete). `.cache/` holds regenerable
artifacts (safe to delete anytime). All commands run from the repo root.

## 1. Stash your current work

Save everything you have in progress before pulling (including untracked
files):

```bash
git stash -u
```

## 2. Pull the migration code

```bash
git pull
```

## 3. Run the migration

```bash
python -m scripts.migrate_rawdata_to_duckdb --reset --force   # import rawData/
```

Re-runs are safe — anything already imported reports `SKIP: already
migrated`. 

```bash
python -m pytest . -q                                         # verify
```

if everything pass, you can purge old data

```bash
python -m scripts.migrate_rawdata_to_duckdb --purge           # remove legacy dirs
```

The purge removes `rawData/`, `output/`, `data/` (13F zips are
salvaged to `.cache/sec_13f/`) and refuses to run unless every dataset was
migrated and verified, so sources are never deleted unless the store already
has them.

## 4. Restore your work

```bash
git stash pop
```

## Conflicts?

Your work is never lost — it's still safe in the stash.

1. Ask your AI assistant to solve the `<<<<<<<` conflict markers based on
   your last changes.
2. If it really can't, reset and try again:

```bash
git reset --hard   # drops the half-applied pop, keeps the stash
git stash pop      # re-apply, then edit the files and remove the markers
git add -A && git stash drop
```
