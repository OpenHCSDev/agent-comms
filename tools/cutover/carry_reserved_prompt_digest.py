"""Give every reserved input the digest of the request it was reserved for.

The digest was a row of a file of its own, ``native_prompt_bindings.sqlite3``: one row per
reserved input, keyed by it. The input's own row holds the digest now, so this copies each
stored digest onto its row and, once the install is accepted, removes the file. The file's
other columns are not carried: six repeat the input row's own identity, the wire root is the
one its claims were accepted under, and nothing read the time it was written.

An input with no stored digest was reserved by a process that died before writing one. No
digest can be made up for it, so the carry refuses and names how many there are.
"""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import ClassVar

from agent_comms.coordination.database import CoordinationStore
from agent_comms.errors import RelationViolationError
from agent_comms.execution.native_runtime_input import NativeRuntimeInput
from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.storage.locked_store import RootFile


class ReservedPromptDigest(Carry):
    opened_files: ClassVar[tuple[type[RootFile], ...]] = (CoordinationStore,)
    added: ClassVar[str] = "expected_prompt_digest"
    previous: ClassVar[str] = "native_prompt_bindings.sqlite3"
    beside: ClassVar[tuple[str, ...]] = (".{name}.snapshot-lock", ".{name}.pending")
    """The retired file's lock and its commit marker, which only it ever had."""

    def stored(self, store: Path) -> Path:
        """The retired file beside one coordinator store."""
        return store.with_name(self.previous)

    def input_files(self) -> tuple[Path, ...]:
        """The retired file is an input like the store: the practice copy holds it, so the
        rehearsal runs this carry, and a failed install puts it back."""
        stores = super().input_files()
        retired = tuple(path for store in stores if (path := self.stored(store)).is_file())
        for path in retired:
            self.require_confined(path)
        return tuple(sorted({*stores, *retired}))

    def digests(self, store: Path) -> dict[str, str]:
        path = self.stored(store)
        if not path.is_file():
            return {}
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            return dict(db.execute(
                'SELECT "input_id", "expected_prompt_digest" FROM "prompt_binding"'))

    def carry(self, *, apply: bool) -> Carried:
        carried: dict[Path, dict[str, str]] = {}
        inputs = 0
        for path in super().input_files():
            with closing(CoordinationStore.read_snapshot(path)) as db:
                if not NativeRuntimeInput.present(db):
                    continue
                if self.added in NativeRuntimeInput.stored_columns(db):
                    continue
                reserved = {input_id for (input_id,) in db.execute(
                    f'SELECT "input_id" FROM "{NativeRuntimeInput.declared_name}"')}
            digests = self.digests(path)
            if unbound := len(reserved.difference(digests)):
                raise RelationViolationError(
                    f"{path.name}: {unbound} of {len(reserved)} reserved inputs have no stored"
                    f" prompt digest in {self.previous}")
            carried[path] = {input_id: digests[input_id] for input_id in reserved}
            inputs += len(reserved)
        if apply:
            for path, digests in carried.items():
                with (
                    closing(CoordinationStore.writer(path)) as db,
                    CoordinationStore.write_transaction(db),
                ):
                    db.execute('CREATE TEMP TABLE "carried_prompt_digest" ('
                               '"input_id" TEXT PRIMARY KEY, "digest" TEXT NOT NULL) WITHOUT ROWID')
                    db.executemany('INSERT INTO "carried_prompt_digest" VALUES (?, ?)',
                                   digests.items())
                    NativeRuntimeInput.rebuild(db, changed={self.added: (
                        '(SELECT "digest" FROM "carried_prompt_digest" c WHERE c."input_id"='
                        f'"{NativeRuntimeInput.declared_name}"."input_id")')})
                    db.execute('DROP TABLE "carried_prompt_digest"')
        return Carried(
            len(carried),
            detail={"stores": len(carried), "inputs": inputs},
            rewritten=tuple(path.relative_to(self.root) for path in carried) if apply else (),
        )

    def after_acceptance(self) -> Carried:
        """A restore needs the file to put the old rows back, so it outlives the rollback
        window. Its lock and its commit marker are not content and go with it."""
        retired = tuple(
            path for store in super().input_files() if (path := self.stored(store)).is_file())
        for path in retired:
            path.unlink()
            for name in self.beside:
                path.with_name(name.format(name=path.name)).unlink(missing_ok=True)
        return Carried(len(retired))
