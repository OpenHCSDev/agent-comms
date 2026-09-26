"""Provider-free, bounded Pi-native source snapshot for a selected operation.

This is only a source witness. Neither an emitted context hash nor a visible
input-proof sidecar settles an earlier raw input, authorizes a paid request, or
replaces the one-use owner/child/credential/terminal checks at STARTED.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fresh_private_session import FreshPrivateSession
from .native_pi import NativePiUnavailable, _private_session_dir, _trusted_package

_MAX_SOURCE_FILE = 16 * 1024 * 1024
_MAX_CONTEXT = 1024 * 1024
_MAX_STDOUT = 2 * 1024 * 1024
# Exact reviewed copied 0.85.1 preparation logic used for the cut point.
_COMPACTION_SHA256 = "3d5f1f2a3e801c965214717b6abad1839239b4a030517bffdf0c8eff25df5c2a"

# This source runs only in the explicitly verified disposable copied Pi package.
# It is deliberately read-only: inMemory avoids SessionManager.open's writer,
# and no model, extension, settings or auth accessor is called.
_SNAPSHOT_JS = r"""
import {createHash} from 'node:crypto';
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
  throw Error('Selected source is not strict v3');
const manager = SessionManager.inMemory(process.cwd(), undefined, rows);
if (manager.getSessionId() !== rows[0].id || !manager.getLeafId())
  throw Error('Selected source has no verified leaf');
const branch = manager.getBranch();
const settings = {...DEFAULT_COMPACTION_SETTINGS, keepRecentTokens:Number(recent)};
const preparation = prepareCompaction(branch, settings);
if (!preparation || preparation.isSplitTurn) {
  console.log(JSON.stringify({status:'skip',reason:preparation?'split_turn':'no_cut'}));
} else {
  if (!branch.some(entry => entry.id === preparation.firstKeptEntryId))
    throw Error('Selected source kept entry changed');
  const content = Buffer.from(JSON.stringify(manager.buildSessionContext()), 'utf8');
  if (!content.length || content.length > 1024*1024)
    throw Error('Selected source context is outside one MiB bound');
  console.log(JSON.stringify({status:'ready',sessionId:manager.getSessionId(),
    leafId:manager.getLeafId(),firstKeptEntryId:preparation.firstKeptEntryId,
    contextDigest:createHash('sha256').update(content).digest('hex'),
    contextBase64:content.toString('base64')}));
}
"""


class SelectedSourceSnapshotError(ValueError):
    """No trusted stable source bytes are available; no selected STARTED."""


_FileRevision = tuple[int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class SelectedSourceSnapshot:
    session_file: Path
    source: dict[str, str]
    sidecar_revision: _FileRevision
    context_bytes: bytes


def _private_revision(path: Path) -> _FileRevision:
    try:
        info = path.lstat()
    except OSError as error:
        raise SelectedSourceSnapshotError("Selected source file is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or not 0 < info.st_size <= _MAX_SOURCE_FILE
    ):
        raise SelectedSourceSnapshotError("Selected source file is not private and bounded")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_snapshot_result(raw: bytes, fresh: FreshPrivateSession, revision: str) -> dict[str, Any]:
    if not raw or len(raw) > _MAX_STDOUT or not raw.endswith(b"\n"):
        raise SelectedSourceSnapshotError("Selected source result is incomplete or oversized")
    try:
        result = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise SelectedSourceSnapshotError("Selected source result is not JSON") from error
    if type(result) is not dict:
        raise SelectedSourceSnapshotError("Selected source result is not a record")
    if (
        set(result) == {"status", "reason"}
        and result["status"] == "skip"
        and result["reason"] in {"split_turn", "no_cut"}
    ):
        return result
    if (
        set(result)
        != {"status", "sessionId", "leafId", "firstKeptEntryId", "contextDigest", "contextBase64"}
        or result["status"] != "ready"
        or result["sessionId"] != fresh.session_id
        or any(
            type(result[key]) is not str or not result[key]
            for key in ("leafId", "firstKeptEntryId", "contextDigest", "contextBase64")
        )
        or len(result["contextDigest"]) != 64
        or any(c not in "0123456789abcdef" for c in result["contextDigest"])
    ):
        raise SelectedSourceSnapshotError("Selected source returned mismatched identity")
    try:
        content = base64.b64decode(result["contextBase64"], validate=True)
        context = json.loads(content)
    except (ValueError, UnicodeError) as error:
        raise SelectedSourceSnapshotError("Selected source context bytes are invalid") from error
    if (
        not 0 < len(content) <= _MAX_CONTEXT
        or hashlib.sha256(content).hexdigest() != result["contextDigest"]
        or type(context) is not dict
        or type(context.get("messages")) is not list
        or not context["messages"]
    ):
        raise SelectedSourceSnapshotError("Selected source context digest or content differs")
    result["source"] = {
        "sessionId": fresh.session_id,
        "revision": revision,
        "leafId": result["leafId"],
        "firstKeptEntryId": result["firstKeptEntryId"],
        "contextDigest": result["contextDigest"],
    }
    result["contextBytes"] = content
    return result


def capture_selected_source_snapshot(
    package: Path,
    fresh: FreshPrivateSession,
    *,
    keep_recent_tokens: int = 20000,
) -> SelectedSourceSnapshot | None:
    """Return an exact source/sidecar revision and Pi-built context, or skip.

    The caller must hold its authoritative owner/wire/native-writer exclusion
    throughout this read and separately require every prior raw-input returned
    retirement receipt. This method is never a recovery/terminal receipt.
    """
    if type(fresh) is not FreshPrivateSession:
        raise SelectedSourceSnapshotError("Returned fresh-session identity required")
    if type(keep_recent_tokens) is not int or not 0 < keep_recent_tokens <= 100_000:
        raise ValueError("Bounded selected context retention required")
    file = fresh.path
    try:
        _trusted_package(Path(package))
        compaction = Path(package) / "dist/core/compaction/compaction.js"
        compaction_info = compaction.lstat()
        if (
            not stat.S_ISREG(compaction_info.st_mode)
            or compaction_info.st_uid != os.geteuid()
            or hashlib.sha256(compaction.read_bytes()).hexdigest() != _COMPACTION_SHA256
        ):
            raise SelectedSourceSnapshotError("Selected Pi preparation module differs")
        _private_session_dir(file.parent)
        if file != file.resolve(strict=True):
            raise SelectedSourceSnapshotError("Selected source has redirected path")
        fresh.verify_saved_identity()
        before = _private_revision(file)
        proof_path = Path(str(file) + ".input-proof")
        proof_before = _private_revision(proof_path)
        if (before[0], before[1]) != (fresh.device, fresh.inode):
            raise SelectedSourceSnapshotError("Selected saved file lost its enrolled inode")
        revision = ":".join(map(str, before))
        node = shutil.which("node")
        if node is None:
            raise SelectedSourceSnapshotError("Verified selected source loader unavailable")
        result = subprocess.run(
            [
                node,
                "--no-global-search-paths",
                "--input-type=module",
                "--eval",
                _SNAPSHOT_JS,
                str(Path(package).absolute()),
                str(file),
                str(keep_recent_tokens),
            ],
            cwd=file.parent,
            env={
                "HOME": str(file.parent),
                "PATH": os.environ.get("PATH", os.defpath),
                "PI_OFFLINE": "1",
                "NODE_DISABLE_COMPILE_CACHE": "1",
            },
            capture_output=True,
            timeout=10,
        )
        if (
            result.returncode
            or _private_revision(file) != before
            or _private_revision(proof_path) != proof_before
        ):
            raise SelectedSourceSnapshotError("Selected source changed during native snapshot")
        fresh.verify_saved_identity()
        parsed = _read_snapshot_result(result.stdout, fresh, revision)
        if parsed["status"] == "skip":
            return None
        return SelectedSourceSnapshot(file, parsed["source"], proof_before, parsed["contextBytes"])
    except (OSError, subprocess.TimeoutExpired, NativePiUnavailable) as error:
        raise SelectedSourceSnapshotError("Selected source cannot be safely captured") from error
