"""Read-only, pinned native preparation before any adaptive summary request.

Only native witness and bounded preparation metadata cross this boundary; no
summary source, prompt, or session content is returned to the caller. This is
not a provider invocation or a native writer. OwnerCompactionCommit must still
capture its canonical source BEFORE summary generation and recheck on commit.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass, field, fields
from pathlib import Path

from .field_codec import FieldCodec
from .native_package import verify_native_package

_PREPARE = r"""
import {realpathSync, lstatSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const [root, file, recent] = process.argv.slice(1);
const {DiskEntryStore} = await import(
  pathToFileURL(join(root, 'dist/core/session-entry-store.js')));
const {prepareCompaction, DEFAULT_COMPACTION_SETTINGS} = await import(
  pathToFileURL(join(root, 'dist/core/compaction/compaction.js')));
const store = new DiskEntryStore(file);
try {
const revision = store.revision;
const settings = recent === 'default' ? DEFAULT_COMPACTION_SETTINGS :
  {...DEFAULT_COMPACTION_SETTINGS, keepRecentTokens: Number(recent)};
const preparation = prepareCompaction(store, settings);
if (!preparation) {
  console.log(JSON.stringify({status:'skip', sessionId:store.header.id}));
} else {
  if (!store.branchContains(store.lastId, preparation.firstKeptEntryId))
    throw new Error('Native kept entry changed');
  const witness = {sessionId:store.header.id,sessionFile:realpathSync(file),
    leafId:store.lastId,firstKeptEntryId:preparation.firstKeptEntryId,revision};
  console.log(JSON.stringify({status:'ready', sessionId:store.header.id,
    witness, tokensBefore:preparation.tokensBefore,
    isSplitTurn:preparation.isSplitTurn}));
}
store.assertCurrent();
} finally {store.close();}
"""


class NativePreparationError(ValueError):
    """No safe preparation was established; never start a summary request."""


@dataclass(frozen=True)
class NativeWitness:
    """One decoded native cutpoint; later owner/disk CAS remains independent."""

    session_id: str = field(metadata={"wire_name": "sessionId"})
    session_file: str = field(metadata={"wire_name": "sessionFile"})
    leaf_id: str = field(metadata={"wire_name": "leafId"})
    first_kept_entry_id: str = field(metadata={"wire_name": "firstKeptEntryId"})
    revision: str

    def __post_init__(self):
        if any(
            type(value := getattr(self, item.name)) is not str or not value for item in fields(self)
        ):
            raise NativePreparationError("Exact native witness required")
        if (
            not self.session_file.startswith("/")
            or re.fullmatch(r"[0-9]+:[0-9]+:[0-9]+:[0-9]+:[0-9]+", self.revision) is None
        ):
            raise NativePreparationError("Canonical native path and revision required")


@dataclass(frozen=True)
class NativePreparation:
    witness: NativeWitness
    tokens_before: int
    is_split_turn: bool


def prepare_native_source(
    package: Path, session_file: str, *, keep_recent_tokens: int | None = None
) -> NativePreparation | None:
    """Derive Pi's actual cut point without a model call or a session mutation.

    A test may explicitly override Pi's recent window; production defaults to
    Pi's declared DEFAULT_COMPACTION_SETTINGS. The returned witness is not
    authority: the owner captures source and the writer later CASes on disk.
    """
    if keep_recent_tokens is not None and (
        type(keep_recent_tokens) is not int or not 0 < keep_recent_tokens <= 10_000_000
    ):
        raise NativePreparationError("Invalid bounded recent context window")
    try:
        package = package.resolve(strict=True)
        verify_native_package(package)
        file = Path(session_file).absolute()
        if file != file.resolve(strict=True):
            raise NativePreparationError("Native session path is not canonical")
        before = file.stat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid not in (0, os.getuid())
            or before.st_size <= 0
        ):
            raise NativePreparationError("Native session is not regular storage")
        node = shutil.which("node")
        if node is None:
            raise NativePreparationError("Native preparer unavailable")
        environment = dict(os.environ)
        for key in ("NODE_OPTIONS", "NODE_PATH", "NODE_COMPILE_CACHE"):
            environment.pop(key, None)
        environment["NODE_DISABLE_COMPILE_CACHE"] = "1"
        environment["PI_OFFLINE"] = "1"
        result = subprocess.run(
            [
                node,
                "--no-global-search-paths",
                "--import",
                str(package / "dist/agent-comms-import-fence.mjs"),
                "--input-type=module",
                "--eval",
                _PREPARE,
                str(package),
                str(file),
                "default" if keep_recent_tokens is None else str(keep_recent_tokens),
            ],
            cwd=file.parent,
            env=environment,
            capture_output=True,
            timeout=10,
        )
        after = file.stat()

        def revision(info: os.stat_result) -> tuple[int, ...]:
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        if result.returncode or revision(before) != revision(after) or len(result.stdout) > 4096:
            raise NativePreparationError("Native preparation failed or changed")
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or type(data.get("sessionId")) is not str:
            raise NativePreparationError("Invalid native preparation")
        if data.get("status") == "skip" and set(data) == {"status", "sessionId"}:
            return None
        if set(data) != {"status", "sessionId", "witness", "tokensBefore", "isSplitTurn"}:
            raise NativePreparationError("Invalid native preparation")
        witness = FieldCodec.decode(NativeWitness, data["witness"])
        if (
            data["status"] != "ready"
            or type(data["tokensBefore"]) is not int
            or not 0 <= data["tokensBefore"] <= 2**53 - 1
            or type(data["isSplitTurn"]) is not bool
            or witness.session_id != data["sessionId"]
            or witness.session_file != str(file)
            or witness.revision != ":".join(map(str, revision(before)))
        ):
            raise NativePreparationError("Invalid native witness")
        return NativePreparation(witness, data["tokensBefore"], data["isSplitTurn"])
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError) as error:
        if isinstance(error, NativePreparationError):
            raise
        raise NativePreparationError("Native source cannot be prepared") from error
