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
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fresh_private_session import FreshPrivateSession
from .native_pi import NativePiUnavailable, _private_session_dir, _trusted_package, _unique

_MAX_SOURCE_FILE = 16 * 1024 * 1024
_MAX_CONTEXT = 1024 * 1024
_MAX_HISTORY = 256 * 1024
_MAX_STDOUT = 2 * 1024 * 1024
# Exact reviewed copied 0.85.1 preparation logic used for the cut point.
_COMPACTION_SHA256 = "3d5f1f2a3e801c965214717b6abad1839239b4a030517bffdf0c8eff25df5c2a"
# Explicit fileOps/footer helper imported by this candidate snapshot. Its
# SHA4489 copied-package digest is a defensive pre-Node deny gate, not a new
# compaction pin or proof of the remaining transitive startup import closure.
_COMPACTION_UTILS_SHA256 = "eba26429c8ed717754bcdc3c1164715b1d2f2d68655530cf1e0df975c18f6891"

# This source runs only after the existing SHA3d compaction pin has passed.
# It is deliberately read-only: inMemory avoids SessionManager.open's writer.
# The parent supplies bytes from its checked FD, never a path for Pi to reopen.
# No model, extension, settings or auth accessor is called.
_SNAPSHOT_JS = r"""
import {createHash} from 'node:crypto';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const [root, recent] = process.argv.slice(1);
const {SessionManager} = await import(
  pathToFileURL(join(root, 'dist/core/session-manager.js')));
const {prepareCompaction, DEFAULT_COMPACTION_SETTINGS} = await import(
  pathToFileURL(join(root, 'dist/core/compaction/compaction.js')));
const {computeFileLists, formatFileOperations} = await import(
  pathToFileURL(join(root, 'dist/core/compaction/utils.js')));
const raw = await (async () => {
  const {readFileSync} = await import('node:fs');
  return readFileSync(0);
})();
if (!raw.length || raw.length > 16*1024*1024 || raw.at(-1) !== 10)
  throw Error('Selected source FD bytes are incomplete or oversized');
const lines = raw.toString('utf8').split('\n');
lines.pop();
const rows = lines.map(line => {
  if (!line) throw Error('Selected source has an empty entry');
  return JSON.parse(line);
});
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
  // This hashes Pi's exact prepared history objects, including prior summary,
  // not an independently attested prompt, stream attempt or source parity.
  const history = Buffer.from(JSON.stringify({
    messagesToSummarize:preparation.messagesToSummarize,
    previousSummary:preparation.previousSummary ?? null,
    turnPrefixMessages:preparation.turnPrefixMessages,
    firstKeptEntryId:preparation.firstKeptEntryId,
  }), 'utf8');
  if (!preparation.messagesToSummarize.length ||
      !history.length || history.length > 256*1024)
    throw Error('Selected prepared history is empty or oversized');
  const content = Buffer.from(JSON.stringify(manager.buildSessionContext()), 'utf8');
  if (!content.length || content.length > 1024*1024)
    throw Error('Selected source context is outside one MiB bound');
  // compact() appends these exact file-operation bytes after model output.
  // The four-key prepared history alone cannot bind that final footer.
  const fileLists = computeFileLists(preparation.fileOps);
  const fileOps = Buffer.from(JSON.stringify(fileLists), 'utf8');
  const footer = Buffer.from(formatFileOperations(
    fileLists.readFiles, fileLists.modifiedFiles), 'utf8');
  if (!fileOps.length || fileOps.length > 256*1024 || footer.length > 256*1024)
    throw Error('Selected source file operations are outside bound');
  console.log(JSON.stringify({status:'ready',sessionId:manager.getSessionId(),
    leafId:manager.getLeafId(),firstKeptEntryId:preparation.firstKeptEntryId,
    contextDigest:createHash('sha256').update(content).digest('hex'),
    contextBase64:content.toString('base64'),
    preparedHistoryDigest:createHash('sha256').update(history).digest('hex'),
    preparedHistoryBase64:history.toString('base64'),
    fileOpsDigest:createHash('sha256').update(fileOps).digest('hex'),
    fileOpsByteLength:fileOps.length,fileOpsBase64:fileOps.toString('base64'),
    footerDigest:createHash('sha256').update(footer).digest('hex'),
    footerByteLength:footer.length,footerBase64:footer.toString('base64')}));
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
    prepared_history_bytes: bytes
    file_ops_bytes: bytes
    footer_bytes: bytes


def _revision(info: os.stat_result) -> _FileRevision:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or not 0 < info.st_size <= _MAX_SOURCE_FILE
    ):
        raise SelectedSourceSnapshotError("Selected source file is not private and bounded")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _private_revision(path: Path) -> _FileRevision:
    try:
        return _revision(path.lstat())
    except OSError as error:
        raise SelectedSourceSnapshotError("Selected source file is unavailable") from error


def _read_bound_fd(fd: int, revision: _FileRevision) -> bytes:
    """Reread the *same* enrolled inode, detecting truncation and growth."""
    if _revision(os.fstat(fd)) != revision:
        raise SelectedSourceSnapshotError("Selected source FD revision changed")
    data = bytearray()
    while len(data) < revision[2]:
        chunk = os.pread(fd, min(65536, revision[2] - len(data)), len(data))
        if not chunk:
            raise SelectedSourceSnapshotError("Selected source FD became incomplete")
        data.extend(chunk)
    if os.pread(fd, 1, revision[2]) or _revision(os.fstat(fd)) != revision:
        raise SelectedSourceSnapshotError("Selected source FD changed during read")
    return bytes(data)


def _open_bound_fd(path: Path, stack: ExitStack) -> tuple[int, _FileRevision, bytes]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise SelectedSourceSnapshotError("Selected source cannot deny redirected opens")
    before = _private_revision(path)
    fd = os.open(
        path,
        os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    stack.callback(os.close, fd)
    if _revision(os.fstat(fd)) != before or _private_revision(path) != before:
        raise SelectedSourceSnapshotError("Selected source path rebounded while opening")
    data = _read_bound_fd(fd, before)
    if _private_revision(path) != before:
        raise SelectedSourceSnapshotError("Selected source path changed during FD read")
    return fd, before, data


def _verify_bound_fd(path: Path, fd: int, revision: _FileRevision, digest: str) -> None:
    if (
        _private_revision(path) != revision
        or hashlib.sha256(_read_bound_fd(fd, revision)).hexdigest() != digest
        or _private_revision(path) != revision
    ):
        raise SelectedSourceSnapshotError("Selected source FD/path/sidecar changed")


def _valid_sorted_file_paths(paths: object) -> bool:
    """Match JS UTF-16 .sort(), declining surrogate paths before UTF-8 encoding."""
    if type(paths) is not list or any(type(path) is not str or not path for path in paths):
        return False
    try:
        return paths == sorted(set(paths), key=lambda path: path.encode("utf-16-be"))
    except UnicodeError:
        return False


def _expected_file_ops_footer(read_files: list[str], modified_files: list[str]) -> str:
    """Reproduce pinned Pi's formatted footer, not a caller-supplied digest."""
    sections = []
    if read_files:
        sections.append("<read-files>\n" + "\n".join(read_files) + "\n</read-files>")
    if modified_files:
        sections.append("<modified-files>\n" + "\n".join(modified_files) + "\n</modified-files>")
    return "\n\n" + "\n\n".join(sections) if sections else ""


def _read_snapshot_result(raw: bytes, fresh: FreshPrivateSession, revision: str) -> dict[str, Any]:
    if not raw or len(raw) > _MAX_STDOUT or not raw.endswith(b"\n"):
        raise SelectedSourceSnapshotError("Selected source result is incomplete or oversized")
    try:
        result = json.loads(raw, object_pairs_hook=_unique)
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
        != {
            "status",
            "sessionId",
            "leafId",
            "firstKeptEntryId",
            "contextDigest",
            "contextBase64",
            "preparedHistoryDigest",
            "preparedHistoryBase64",
            "fileOpsDigest",
            "fileOpsByteLength",
            "fileOpsBase64",
            "footerDigest",
            "footerByteLength",
            "footerBase64",
        }
        or result["status"] != "ready"
        or result["sessionId"] != fresh.session_id
        or any(
            type(result[key]) is not str or not result[key]
            for key in (
                "leafId",
                "firstKeptEntryId",
                "contextDigest",
                "contextBase64",
                "preparedHistoryDigest",
                "preparedHistoryBase64",
                "fileOpsDigest",
                "fileOpsBase64",
                "footerDigest",
            )
        )
        or type(result["footerBase64"]) is not str
        or any(
            len(result[key]) != 64 or any(c not in "0123456789abcdef" for c in result[key])
            for key in ("contextDigest", "preparedHistoryDigest", "fileOpsDigest", "footerDigest")
        )
        or any(
            type(result[key]) is not int or not 0 <= result[key] <= _MAX_HISTORY
            for key in ("fileOpsByteLength", "footerByteLength")
        )
    ):
        raise SelectedSourceSnapshotError("Selected source returned mismatched identity")
    try:
        content = base64.b64decode(result["contextBase64"], validate=True)
        context = json.loads(content, object_pairs_hook=_unique)
        history = base64.b64decode(result["preparedHistoryBase64"], validate=True)
        prepared = json.loads(history, object_pairs_hook=_unique)
        file_ops_bytes = base64.b64decode(result["fileOpsBase64"], validate=True)
        file_ops = json.loads(file_ops_bytes, object_pairs_hook=_unique)
        footer_bytes = base64.b64decode(result["footerBase64"], validate=True)
        footer_text = footer_bytes.decode("utf-8")
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
    if (
        not 0 < len(history) <= _MAX_HISTORY
        or hashlib.sha256(history).hexdigest() != result["preparedHistoryDigest"]
        or type(prepared) is not dict
        or set(prepared)
        != {"messagesToSummarize", "previousSummary", "turnPrefixMessages", "firstKeptEntryId"}
        or type(prepared["messagesToSummarize"]) is not list
        or not prepared["messagesToSummarize"]
        or any(type(message) is not dict for message in prepared["messagesToSummarize"])
        or prepared["turnPrefixMessages"] != []
        or prepared["firstKeptEntryId"] != result["firstKeptEntryId"]
        or (
            prepared["previousSummary"] is not None and type(prepared["previousSummary"]) is not str
        )
    ):
        raise SelectedSourceSnapshotError("Selected prepared history differs")
    if (
        not 0 < len(file_ops_bytes) <= _MAX_HISTORY
        or len(file_ops_bytes) != result["fileOpsByteLength"]
        or hashlib.sha256(file_ops_bytes).hexdigest() != result["fileOpsDigest"]
        or type(file_ops) is not dict
        or list(file_ops) != ["readFiles", "modifiedFiles"]
        or any(
            not _valid_sorted_file_paths(file_ops[key]) for key in ("readFiles", "modifiedFiles")
        )
        or set(file_ops["readFiles"]) & set(file_ops["modifiedFiles"])
        or json.dumps(file_ops, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        != file_ops_bytes
        or len(footer_bytes) != result["footerByteLength"]
        or len(footer_bytes) > _MAX_HISTORY
        or hashlib.sha256(footer_bytes).hexdigest() != result["footerDigest"]
        or footer_text
        != _expected_file_ops_footer(file_ops["readFiles"], file_ops["modifiedFiles"])
    ):
        raise SelectedSourceSnapshotError("Selected file operations or footer differs")
    model = context.get("model")
    level = context.get("thinkingLevel")
    # These fields are projected by the verified Pi SessionManager from the
    # saved branch, not accepted from request metadata or ambient settings.
    if (
        type(model) is not dict
        or set(model) != {"provider", "modelId"}
        or model["provider"] != "openrouter"
        or model["modelId"] != "z-ai/glm-5.3-flash"
        or type(level) is not str
        or level not in {"low", "high"}
    ):
        raise SelectedSourceSnapshotError(
            "Selected source model/thinking is not explicitly supported"
        )
    result["source"] = {
        "sessionId": fresh.session_id,
        "revision": revision,
        "leafId": result["leafId"],
        "firstKeptEntryId": result["firstKeptEntryId"],
        "contextDigest": result["contextDigest"],
        "preparedHistoryDigest": result["preparedHistoryDigest"],
        "fileOpsDigest": result["fileOpsDigest"],
        "footerDigest": result["footerDigest"],
        "selectedProvider": model["provider"],
        "selectedModelId": model["modelId"],
        "selectedThinkingLevel": level,
    }
    result["contextBytes"] = content
    result["preparedHistoryBytes"] = history
    result["fileOpsBytes"] = file_ops_bytes
    result["footerBytes"] = footer_bytes
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
    retirement receipt. A change after attempt reservation remains UNKNOWN;
    this snapshot cannot clear or retry it. This is never a terminal receipt.
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
        utils = Path(package) / "dist/core/compaction/utils.js"
        utils_info = utils.lstat()
        if (
            not stat.S_ISREG(utils_info.st_mode)
            or utils_info.st_uid != os.geteuid()
            or hashlib.sha256(utils.read_bytes()).hexdigest() != _COMPACTION_UTILS_SHA256
        ):
            raise SelectedSourceSnapshotError("Selected Pi file-operations module differs")
        _private_session_dir(file.parent)
        if file != file.resolve(strict=True):
            raise SelectedSourceSnapshotError("Selected source has redirected path")
        fresh.verify_saved_identity()
        proof_path = Path(str(file) + ".input-proof")
        with ExitStack() as stack:
            source_fd, before, source_data = _open_bound_fd(file, stack)
            proof_fd, proof_before, proof_data = _open_bound_fd(proof_path, stack)
            if (before[0], before[1]) != (fresh.device, fresh.inode):
                raise SelectedSourceSnapshotError("Selected saved file lost its enrolled inode")
            if not source_data.endswith(b"\n") or not proof_data.endswith(b"\n"):
                raise SelectedSourceSnapshotError("Selected source or sidecar is incomplete")
            # Reject malformed input before any Node import. These are exact
            # enrolled FD bytes, not a second read of a mutable pathname.
            try:
                rows = [
                    json.loads(line, object_pairs_hook=_unique)
                    for line in source_data[:-1].decode("utf-8").split("\n")
                ]
            except (UnicodeError, ValueError, TypeError) as error:
                raise SelectedSourceSnapshotError(
                    "Selected source FD entries are invalid"
                ) from error
            if not rows or any(type(row) is not dict for row in rows):
                raise SelectedSourceSnapshotError("Selected source FD entries are invalid")
            source_digest = hashlib.sha256(source_data).hexdigest()
            proof_digest = hashlib.sha256(proof_data).hexdigest()
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
                    str(keep_recent_tokens),
                ],
                input=source_data,
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
            # Even skip/failure must not launder a changed source into a retry.
            try:
                _verify_bound_fd(file, source_fd, before, source_digest)
                _verify_bound_fd(proof_path, proof_fd, proof_before, proof_digest)
            except SelectedSourceSnapshotError as error:
                raise SelectedSourceSnapshotError(
                    "Selected source changed during native snapshot"
                ) from error
            if result.returncode:
                raise SelectedSourceSnapshotError("Selected source changed during native snapshot")
            fresh.verify_saved_identity()
            parsed = _read_snapshot_result(result.stdout, fresh, revision)
            if parsed["status"] == "skip":
                _verify_bound_fd(file, source_fd, before, source_digest)
                _verify_bound_fd(proof_path, proof_fd, proof_before, proof_digest)
                return None
            parsed["source"].update(
                {
                    "sourceDigest": source_digest,
                    "sidecarRevision": ":".join(map(str, proof_before)),
                    "sidecarDigest": proof_digest,
                }
            )
            _verify_bound_fd(file, source_fd, before, source_digest)
            _verify_bound_fd(proof_path, proof_fd, proof_before, proof_digest)
            fresh.verify_saved_identity()
            return SelectedSourceSnapshot(
                file,
                parsed["source"],
                proof_before,
                parsed["contextBytes"],
                parsed["preparedHistoryBytes"],
                parsed["fileOpsBytes"],
                parsed["footerBytes"],
            )
    except (OSError, subprocess.TimeoutExpired, NativePiUnavailable) as error:
        raise SelectedSourceSnapshotError("Selected source cannot be safely captured") from error
