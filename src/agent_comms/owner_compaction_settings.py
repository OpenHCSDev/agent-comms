"""Read Pi's effective compaction settings without its mutable settings storage.

This is trigger evidence only, not owner authority, source capture, or a
provider request. A future ACP caller must bind the selected model/window and
recheck its source before a native commit; nothing invokes this automatically.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .native_package import verify_native_package

_READ_SETTINGS = r"""
import {lstatSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const [root, cwd, contextTokensText, contextWindowText] = process.argv.slice(1);
const {CONFIG_DIR_NAME, getAgentDir} = await import(pathToFileURL(join(root, 'dist/config.js')));
const {SettingsManager} = await import(
  pathToFileURL(join(root, 'dist/core/settings-manager.js')));
const {shouldCompact} = await import(
  pathToFileURL(join(root, 'dist/core/compaction/compaction.js')));
const contextTokens = Number(contextTokensText), contextWindow = Number(contextWindowText);
if (!Number.isSafeInteger(contextTokens) || contextTokens < 0 ||
    !Number.isSafeInteger(contextWindow) || contextWindow <= 0)
  throw new Error('Invalid trigger token/window evidence');
// Pi's migration, merge and effective defaults are authoritative. Its normal
// FileSettingsStorage takes and writes lock files even for reads; this custom
// storage exposes *only* immutable read callbacks, and refuses write attempts.
const storage = {withLock(scope, callback) {
  const path = scope === 'global' ? join(getAgentDir(), 'settings.json') :
    scope === 'project' ? join(cwd, CONFIG_DIR_NAME, 'settings.json') : null;
  if (!path) throw new Error('Invalid settings scope');
  let raw;
  try {
    const before = lstatSync(path, {bigint:true});
    if (!before.isFile() || before.nlink !== 1n || before.size > 1048576n)
      throw new Error('Settings must be a bounded regular file');
    raw = readFileSync(path, 'utf8');
    const after = lstatSync(path, {bigint:true});
    if (before.dev !== after.dev || before.ino !== after.ino ||
        before.size !== after.size || before.mtimeNs !== after.mtimeNs ||
        before.ctimeNs !== after.ctimeNs)
      throw new Error('Settings changed during read');
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
  }
  if (callback(raw) !== undefined) throw new Error('Settings read attempted a write');
}};
const manager = SettingsManager.fromStorage(storage, {projectTrusted:true});
if (manager.globalSettingsLoadError || manager.projectSettingsLoadError)
  throw new Error('Pi settings cannot be loaded without fallback');
const settings = manager.getCompactionSettings();
if (typeof settings.enabled !== 'boolean' ||
    !Number.isSafeInteger(settings.reserveTokens) || settings.reserveTokens < 0 ||
    settings.reserveTokens > 10000000 ||
    !Number.isSafeInteger(settings.keepRecentTokens) ||
    settings.keepRecentTokens <= 0 || settings.keepRecentTokens > 10000000)
  throw new Error('Invalid effective Pi compaction settings');
console.log(JSON.stringify({enabled:settings.enabled,
  reserveTokens:settings.reserveTokens, keepRecentTokens:settings.keepRecentTokens,
  trigger:shouldCompact(contextTokens,contextWindow,settings)}));
"""


class PiSettingsEvidenceError(ValueError):
    """Effective Pi settings could not be read without mutation; skip trigger."""


@dataclass(frozen=True)
class PiCompactionDecision:
    enabled: bool
    reserve_tokens: int
    keep_recent_tokens: int
    trigger: bool


def read_compaction_decision(
    package: Path, worktree: str, *, context_tokens: int, context_window: int
) -> PiCompactionDecision:
    """Evaluate Pi's declared trigger on bounded evidence without mutating Pi.

    This returns no grant to generate a summary or write a session. The future
    owner caller must separately capture/recheck turn, model and ingress.
    """
    if (
        type(context_tokens) is not int
        or not 0 <= context_tokens <= 2**53 - 1
        or type(context_window) is not int
        or not 0 < context_window <= 2**53 - 1
    ):
        raise PiSettingsEvidenceError("Invalid selected-model context evidence")
    try:
        package = package.resolve(strict=True)
        verify_native_package(package)
        cwd = Path(worktree).absolute()
        if cwd != cwd.resolve(strict=True) or not cwd.is_dir():
            raise PiSettingsEvidenceError("Worktree is not canonical")
        node = shutil.which("node")
        if node is None:
            raise PiSettingsEvidenceError("Pinned Pi settings reader unavailable")
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
                _READ_SETTINGS,
                str(package),
                str(cwd),
                str(context_tokens),
                str(context_window),
            ],
            cwd=cwd,
            env=environment,
            capture_output=True,
            timeout=10,
        )
        if result.returncode or len(result.stdout) > 1024:
            raise PiSettingsEvidenceError("Pi settings reader refused")
        data = json.loads(result.stdout)
        if (
            not isinstance(data, dict)
            or set(data) != {"enabled", "reserveTokens", "keepRecentTokens", "trigger"}
            or type(data["enabled"]) is not bool
            or type(data["trigger"]) is not bool
            or type(data["reserveTokens"]) is not int
            or not 0 <= data["reserveTokens"] <= 10_000_000
            or type(data["keepRecentTokens"]) is not int
            or not 0 < data["keepRecentTokens"] <= 10_000_000
        ):
            raise PiSettingsEvidenceError("Invalid effective Pi compaction decision")
        return PiCompactionDecision(
            data["enabled"], data["reserveTokens"], data["keepRecentTokens"], data["trigger"]
        )
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, PiSettingsEvidenceError):
            raise
        raise PiSettingsEvidenceError("Pi settings decision unavailable") from error
