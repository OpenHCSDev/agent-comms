"""Reset disposable rule reviews after removing their unread citation flag."""

from contextlib import closing
from pathlib import Path

from agent_comms.coordination_database import CoordinationStore
from agent_comms.rule_reviews import RuleReviews
from agent_comms.typed_table import SQLiteSchemaObject

from .carry import Carried, StoreCarry


class ResetRuleReviews(StoreCarry):
    store = RuleReviews

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        path = cls.store.path_in(root)
        if not path.exists():
            return Carried(0)
        with closing(CoordinationStore.writer(path)) as db:
            if not SQLiteSchemaObject.differences(
                cls.store._schema(db), cls.store.declared_schema()
            ):
                return Carried(0)
            if apply:
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    for row in cls.store.rows:
                        row.drop(db)
                        row.create(db)
        return Carried(1, rewritten=(path.name,) if apply else ())


if __name__ == "__main__":
    ResetRuleReviews.main()
