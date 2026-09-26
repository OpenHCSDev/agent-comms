"""Read-only, pinned native preparation before any adaptive summary request.

Only native witness and bounded preparation metadata cross this boundary; no
summary source, prompt, or session content is returned to the caller. This is
not a provider invocation or a native writer. OwnerCompactionCommit must still
capture its canonical source BEFORE summary generation and recheck on commit.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .native_package import verify_native_package

_PREPARE = r"""
import {realpathSync, lstatSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const [root, file, recent] = process.argv.slice(1);
const {loadEntriesFromFile, SessionManager} = await import(
  pathToFileURL(join(root, 'dist/core/session-manager.js')));
const {prepareCompaction, DEFAULT_COMPACTION_SETTINGS} = await import(
  pathToFileURL(join(root, 'dist/core/compaction/compaction.js')));
const rows = loadEntriesFromFile(file);
if (!rows.length || rows[0].type !== 'session' || rows[0].version !== 3 ||
    typeof rows[0].id !== 'string' || !rows[0].id)
  throw new Error('Native session is not strict v3');
// In-memory loader is intentional: SessionManager.open() has an empty-file
// initialization writer if the path races. Preparation must NEVER own a writer.
const manager = SessionManager.inMemory(process.cwd(), undefined, rows);
if (manager.getSessionId() !== rows[0].id || !manager.getLeafId())
  throw new Error('Native session identity changed');
const stat = lstatSync(file, {bigint:true});
if (!stat.isFile() || stat.nlink !== 1n || stat.size > 256n*1024n*1024n)
  throw new Error('Native session revision unavailable');
// Mirrors the exact pinned pr48DiskRevision tuple. Writer CAS independently
// recomputes it under the session lock; this is evidence, never authority.
const revision = [stat.dev,stat.ino,stat.size,stat.mtimeNs,stat.ctimeNs]
  .map(String).join(':');
const settings = recent === 'default' ? DEFAULT_COMPACTION_SETTINGS :
  {...DEFAULT_COMPACTION_SETTINGS, keepRecentTokens: Number(recent)};
const preparation = prepareCompaction(manager.getBranch(), settings);
if (!preparation) {
  console.log(JSON.stringify({status:'skip', sessionId:rows[0].id}));
} else {
  if (!manager.getBranch().some(entry => entry.id === preparation.firstKeptEntryId))
    throw new Error('Native kept entry changed');
  const witness = {sessionId:manager.getSessionId(),sessionFile:realpathSync(file),
    leafId:manager.getLeafId(),firstKeptEntryId:preparation.firstKeptEntryId,revision};
  console.log(JSON.stringify({status:'ready', sessionId:rows[0].id,
    witness, tokensBefore:preparation.tokensBefore,
    isSplitTurn:preparation.isSplitTurn}));
}
"""


class NativePreparationError(ValueError):
    """No safe preparation was established; never start a summary request."""


@dataclass(frozen=True)
class NativePreparation:
    session_id: str
    witness: dict[str, str]
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
            or not 0 < before.st_size <= 256 * 1024 * 1024
        ):
            raise NativePreparationError("Native session is not bounded regular storage")
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
        witness = data["witness"]
        if (
            data["status"] != "ready"
            or type(data["tokensBefore"]) is not int
            or not 0 <= data["tokensBefore"] <= 2**53 - 1
            or type(data["isSplitTurn"]) is not bool
            or not isinstance(witness, dict)
            or set(witness)
            != {"sessionId", "sessionFile", "leafId", "firstKeptEntryId", "revision"}
            or any(type(value) is not str or not value for value in witness.values())
            or witness["sessionId"] != data["sessionId"]
            or witness["sessionFile"] != str(file)
            or witness["revision"] != ":".join(map(str, revision(before)))
        ):
            raise NativePreparationError("Invalid native witness")
        return NativePreparation(
            data["sessionId"], witness, data["tokensBefore"], data["isSplitTurn"]
        )
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, NativePreparationError):
            raise
        raise NativePreparationError("Native source cannot be prepared") from error
