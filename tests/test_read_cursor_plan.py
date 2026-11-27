"""The unread join finds a row's cursors by target; JSON columns carry no affinity for it."""

import sqlite3

from agent_comms.messaging.read_basis import ReadCursor


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


def test_unread_exceptions_materialize_once_and_filter_exact_sequences():
    db = sqlite3.connect(":memory:")
    db.execute('CREATE TABLE addressed (seq INTEGER PRIMARY KEY, sender TEXT NOT NULL, target TEXT NOT NULL) STRICT')
    db.executemany(
        "INSERT INTO addressed (seq, sender, target) VALUES (?, ?, ?)",
        [(1, "alice", "#comms"), (2, "bob", "#comms"), (3, "alice", "#comms"),
         (4, "alice", "viewer"), (5, "alice", "viewer"), (6, "bob", "viewer")],
    )
    cursors_json = (
        '[["viewer","#comms","",1,[3]],'
        '["viewer","viewer","alice",4,[5]]]'
    )
    plan = " ".join(row[3] for row in db.execute(
        f"EXPLAIN QUERY PLAN WITH {ReadCursor.sql('?')}, "
        "requests AS (SELECT 'viewer' AS actor,* FROM addressed), "
        f"{ReadCursor.joined_sql} SELECT seq FROM cursors WHERE {ReadCursor.unread_sql}",
        (cursors_json,),
    ))
    assert "MATERIALIZE cursor_exceptions" in plan, plan
    assert "CORRELATED LIST SUBQUERY" not in plan, plan
    unread = [row[0] for row in db.execute(
        f"WITH {ReadCursor.sql('?')}, "
        "requests AS (SELECT 'viewer' AS actor,* FROM addressed), "
        f"{ReadCursor.joined_sql} SELECT seq FROM cursors WHERE {ReadCursor.unread_sql} ORDER BY seq",
        (cursors_json,),
    )]
    assert unread == [2, 6]
