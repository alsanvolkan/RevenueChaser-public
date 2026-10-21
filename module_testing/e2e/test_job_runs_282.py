"""A finished job's ``job_runs`` row is in the store, and the file is free, by the time ``/api/job`` says so (#282)."""
from __future__ import annotations


def test_a_done_job_has_its_row_and_the_store_is_free_to_open(dashboard):
    import duckdb

    result = dashboard.run_job("/run/moneyflow")
    assert result.ok, result
    # Plain connect, no retry: the dashboard must have let go of the file before it reported "done".
    conn = duckdb.connect(str(dashboard.store_dir / "raw.duckdb"), read_only=True)
    try:
        rows = conn.execute("SELECT status, module, json_type(params) FROM job_runs WHERE run_id = ?",
                            [result.id]).fetchall()
    finally:
        conn.close()
    assert rows == [("done", "moneyflow", "OBJECT")]
