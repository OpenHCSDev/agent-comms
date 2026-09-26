"""One bounded Pi-native summarization request, without native session writers.

This is invoked only after the owner has captured its pre-summary source. The
child reads the same saved session in memory; it cannot commit the result.
Provider errors, absent credentials, cancellation and timeouts never retry.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from .native_package import verify_native_package
from .owner_compaction_prepare import NativePreparation

_SUMMARIZE = r"""
import {lstatSync, realpathSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const [root, file, expected, provider, modelId, recent, reserve, window] = process.argv.slice(1);
const {loadEntriesFromFile, SessionManager} = await import(
  pathToFileURL(join(root, 'dist/core/session-manager.js')));
const {prepareCompaction, compact, DEFAULT_COMPACTION_SETTINGS} = await import(
  pathToFileURL(join(root, 'dist/core/compaction/compaction.js')));
const {ModelRuntime} = await import(pathToFileURL(join(root, 'dist/core/model-runtime.js')));
const {streamSimple} = await import(pathToFileURL(
  join(root, 'node_modules/@earendil-works/pi-ai/dist/compat.js')));
const witness = JSON.parse(expected);
const stat = lstatSync(file, {bigint:true});
if (!stat.isFile() || stat.nlink !== 1n || stat.size > 256n*1024n*1024n)
  throw new Error('Invalid native source');
const revision = [stat.dev,stat.ino,stat.size,stat.mtimeNs,stat.ctimeNs].map(String).join(':');
if (revision !== witness.revision || realpathSync(file) !== witness.sessionFile)
  throw new Error('Native source changed before provider request');
const rows = loadEntriesFromFile(file);
if (!rows.length || rows[0].type !== 'session' || rows[0].version !== 3 ||
    rows[0].id !== witness.sessionId) throw new Error('Invalid saved session');
const manager = SessionManager.inMemory(process.cwd(), undefined, rows);
const settings = {...DEFAULT_COMPACTION_SETTINGS,
  keepRecentTokens:Number(recent), reserveTokens:Number(reserve)};
if (!Number.isSafeInteger(settings.keepRecentTokens) || settings.keepRecentTokens <= 0 ||
    !Number.isSafeInteger(settings.reserveTokens) || settings.reserveTokens < 0)
  throw new Error('Invalid effective settings');
const preparation = prepareCompaction(manager.getBranch(), settings);
if (!preparation || manager.getSessionId() !== witness.sessionId ||
    manager.getLeafId() !== witness.leafId ||
    preparation.firstKeptEntryId !== witness.firstKeptEntryId)
  throw new Error('Native cut point changed before provider request');
const modelRuntime = await ModelRuntime.create({modelsPath:null, refreshOnCreate:false,
  allowModelNetwork:false});
const model = modelRuntime.getModel(provider, modelId);
if (!model || model.contextWindow !== Number(window))
  throw new Error('Selected model context window changed');
const auth = await modelRuntime.getAuth(model);
if (!auth?.auth) throw new Error('Selected model credentials unavailable');
const requestModel = auth.auth.baseUrl ? {...model, baseUrl:auth.auth.baseUrl} : model;
const stop = new AbortController();
const timer = setTimeout(() => stop.abort(), 80000);
let requests = 0;
function boundedStream(selected, context, options) {
  if (++requests > 4) throw new Error('Compaction provider request ceiling exceeded');
  return streamSimple(selected, context, options);
}
try {
  const result = await compact(preparation, requestModel, auth.auth.apiKey,
    auth.auth.headers, undefined, stop.signal, 'low', boundedStream, auth.env,
    {enabled:false,maxRetries:0,provider:{maxRetries:0}}, undefined, undefined);
  const summary = result?.summary;
  if (typeof summary !== 'string' || !summary.trim() ||
      Buffer.byteLength(summary,'utf8') > 262144)
    throw new Error('Invalid bounded Pi summary');
  const out = JSON.stringify({summary});
  if (Buffer.byteLength(out,'utf8') > 270000) throw new Error('Summary envelope too large');
  process.stdout.write(out);
} finally { clearTimeout(timer); }
"""


class NativeSummaryError(ValueError):
    """No summary was obtained; no native commit was authorized."""


async def summarize_native(
    package: Path,
    preparation: NativePreparation,
    *,
    provider: str,
    model_id: str,
    context_window: int,
    reserve_tokens: int,
    keep_recent_tokens: int,
) -> str:
    """Generate one result from a verified Pi package; no provider retry or disk writer."""
    if (
        not provider
        or not model_id
        or type(context_window) is not int
        or context_window <= 0
        or type(reserve_tokens) is not int
        or not 0 <= reserve_tokens <= 10_000_000
        or type(keep_recent_tokens) is not int
        or not 0 < keep_recent_tokens <= 10_000_000
    ):
        raise NativeSummaryError("Invalid selected model or effective settings")
    verify_native_package(package)
    node = shutil.which("node")
    if not node:
        raise NativeSummaryError("Native summarizer unavailable")
    env = dict(os.environ)
    for key in ("NODE_OPTIONS", "NODE_PATH", "NODE_COMPILE_CACHE"):
        env.pop(key, None)
    env["NODE_DISABLE_COMPILE_CACHE"] = "1"
    args = (
        node,
        "--no-global-search-paths",
        "--import",
        str(package / "dist/agent-comms-import-fence.mjs"),
        "--input-type=module",
        "--eval",
        _SUMMARIZE,
        str(package),
        preparation.witness["sessionFile"],
        json.dumps(preparation.witness, separators=(",", ":")),
        provider,
        model_id,
        str(keep_recent_tokens),
        str(reserve_tokens),
        str(context_window),
    )
    child = await asyncio.create_subprocess_exec(
        *args,
        cwd=Path(preparation.witness["sessionFile"]).parent,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert child.stdout is not None
        chunks: list[bytes] = []
        total = 0
        async with asyncio.timeout(90):
            while True:
                part = await child.stdout.read(min(65536, 270001 - total))
                if not part:
                    break
                total += len(part)
                if total > 270000:
                    raise NativeSummaryError("Native summary exceeds transport limit")
                chunks.append(part)
            await asyncio.wait_for(child.wait(), timeout=2)
        raw = b"".join(chunks)
        if child.returncode != 0:
            raise NativeSummaryError("Native summarization refused")
        result = json.loads(raw)
        if set(result) != {"summary"} or type(result["summary"]) is not str:
            raise NativeSummaryError("Invalid native summary envelope")
        summary = result["summary"]
        if not summary.strip() or len(summary.encode()) > 262144:
            raise NativeSummaryError("Invalid bounded native summary")
        return summary
    except BaseException:
        if child.returncode is None:
            child.kill()
            # A cancelled provider transport cannot authorize a native write.
            while child.returncode is None:
                try:
                    await child.wait()
                except asyncio.CancelledError:
                    continue
        raise
