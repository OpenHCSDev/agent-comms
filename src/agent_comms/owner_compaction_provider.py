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
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any

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
  const details = result?.details;
  const paths = values => Array.isArray(values) && values.length <= 256 &&
    values.every(value => typeof value === 'string' && value.length > 0 &&
      Buffer.byteLength(value,'utf8') <= 4096 && !value.includes('\\0'));
  const usage = result?.usage;
  const nonnegative = value => typeof value === 'number' &&
    Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER;
  const counters = ['input','output','cacheRead','cacheWrite','totalTokens'];
  const costs = ['input','output','cacheRead','cacheWrite','total'];
  if (typeof summary !== 'string' || !summary.trim() ||
      Buffer.byteLength(summary,'utf8') > 262144 || !details ||
      Object.keys(details).sort().join(',') !== 'modifiedFiles,readFiles' ||
      !paths(details.readFiles) || !paths(details.modifiedFiles) ||
      !usage || typeof usage !== 'object' || Array.isArray(usage) ||
      Object.keys(usage).some(key =>
        ![...counters,'cost','reasoning','cacheWrite1h'].includes(key)) ||
      counters.some(key => !Number.isSafeInteger(usage[key]) || usage[key] < 0) ||
      ['reasoning','cacheWrite1h'].some(key => usage[key] !== undefined &&
        (!Number.isSafeInteger(usage[key]) || usage[key] < 0)) ||
      !usage.cost || typeof usage.cost !== 'object' || Array.isArray(usage.cost) ||
      Object.keys(usage.cost).sort().join(',') !== costs.sort().join(',') ||
      costs.some(key => !nonnegative(usage.cost[key])))
    throw new Error('Invalid bounded Pi summary, file operations or usage');
  const out = JSON.stringify({summary, details, usage});
  if (Buffer.byteLength(out,'utf8') > 270000) throw new Error('Summary envelope too large');
  process.stdout.write(out);
} finally { clearTimeout(timer); }
"""


class NativeSummaryError(ValueError):
    """No summary was obtained; no native commit was authorized."""


def valid_native_usage(value: Any) -> bool:
    if (
        type(value) is not dict
        or not {"input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost"} <= value.keys()
        or set(value)
        - {
            "input",
            "output",
            "cacheRead",
            "cacheWrite",
            "totalTokens",
            "cost",
            "reasoning",
            "cacheWrite1h",
        }
    ):
        return False
    counters = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")
    if any(type(value[key]) is not int or not 0 <= value[key] <= 2**53 - 1 for key in counters):
        return False
    if any(
        key in value and (type(value[key]) is not int or not 0 <= value[key] <= 2**53 - 1)
        for key in ("reasoning", "cacheWrite1h")
    ):
        return False
    cost = value["cost"]
    if type(cost) is not dict or set(cost) != {
        "input",
        "output",
        "cacheRead",
        "cacheWrite",
        "total",
    }:
        return False
    return all(
        type(amount) in (int, float) and isfinite(amount) and 0 <= amount <= 2**53 - 1
        for amount in cost.values()
    )


@dataclass(frozen=True)
class NativeSummary:
    text: str
    details: dict[str, list[str]]
    usage: dict[str, Any]


async def summarize_native(
    package: Path,
    preparation: NativePreparation,
    *,
    provider: str,
    model_id: str,
    context_window: int,
    reserve_tokens: int,
    keep_recent_tokens: int,
) -> NativeSummary:
    """Generate bounded Pi summary and file operations without a disk writer."""
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
        if (
            type(result) is not dict
            or set(result) != {"summary", "details", "usage"}
            or type(result["summary"]) is not str
        ):
            raise NativeSummaryError("Invalid native summary envelope")
        summary = result["summary"]
        details = result["details"]
        if not summary.strip() or len(summary.encode()) > 262144:
            raise NativeSummaryError("Invalid bounded native summary")
        if not valid_native_usage(result["usage"]):
            raise NativeSummaryError("Invalid bounded native usage")
        if (
            type(details) is not dict
            or set(details) != {"readFiles", "modifiedFiles"}
            or any(
                type(paths) is not list
                or len(paths) > 256
                or any(
                    type(path) is not str or not path or len(path.encode()) > 4096 or "\\0" in path
                    for path in paths
                )
                for paths in details.values()
            )
        ):
            raise NativeSummaryError("Invalid bounded native file operations")
        return NativeSummary(summary, details, result["usage"])
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
