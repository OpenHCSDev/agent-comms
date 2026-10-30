"""The unread join finds a row's cursors by target; JSON columns carry no affinity for it."""

import sqlite3

from agent_comms.read_basis import ReadCursor


def test_the_unread_join_searches_cursors_by_target():
    db = sqlite3.connect(":memory:")
    db.execute('CREATE TABLE addressed (seq INTEGER PRIMARY KEY, sender TEXT NOT NULL, target TEXT NOT NULL) STRICT')
    plan = " ".join(row[3] for row in db.execute(
        f"EXPLAIN QUERY PLAN WITH {ReadCursor.sql('?')}, "
        "requests AS (SELECT 'viewer' AS actor,* FROM addressed), "
        f"{ReadCursor.joined_sql} SELECT count(*) FROM cursors WHERE {ReadCursor.unread_sql}",
        ("[]",),
    ))
    assert "(actor=? AND target=?)" in plan, plan
