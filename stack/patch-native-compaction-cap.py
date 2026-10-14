#!/usr/bin/env python3
"""Make Pi's existing compaction budget the hard Core context policy.

``CompactionSettings`` already owns the retained payload budgets used by
threshold reservation, summary preparation, and recovery. Extend that owner
with one immutable 300,000-token retained-context threshold. SettingsManager
derives every managed session's effective settings from it, so a stale or
per-thread ``compaction.enabled: false`` cannot bypass the Core policy.

Pi still owns when and how compaction runs: ``shouldCompact`` is its existing
threshold decision, and AgentSession invokes it only at completed input/tool
boundaries or after a completed response. Manual compaction and overflow
recovery remain Pi's existing paths. Model metadata keeps reporting each
provider's actual advertised window.
"""

import sys
from pathlib import Path


COMPACTION_JS_STOCK = """export const DEFAULT_COMPACTION_SETTINGS = {
    enabled: true,
    reserveTokens: 16384,
    keepRecentTokens: 20000,
};"""
COMPACTION_JS_OWNED = """// Core's retained-context payload budget. This is the sole declaration of the
// global threshold; model context windows remain provider metadata.
export const DEFAULT_COMPACTION_SETTINGS = Object.freeze({
    enabled: true,
    reserveTokens: 16384,
    keepRecentTokens: 20000,
    retainedContextTokens: 300000,
});"""

SHOULD_COMPACT_STOCK = """export function shouldCompact(contextTokens, contextWindow, settings) {
    if (!settings.enabled)
        return false;
    return contextTokens > contextWindow - settings.reserveTokens;
}"""
SHOULD_COMPACT_OWNED = """export function shouldCompact(contextTokens, contextWindow, settings) {
    if (!settings.enabled)
        return false;
    return contextTokens > Math.min(
        contextWindow - settings.reserveTokens,
        settings.retainedContextTokens,
    );
}"""

COMPACTION_DTS_STOCK = """export interface CompactionSettings {
    enabled: boolean;
    reserveTokens: number;
    keepRecentTokens: number;
}
export declare const DEFAULT_COMPACTION_SETTINGS: CompactionSettings;"""
COMPACTION_DTS_OWNED = """export interface CompactionSettings {
    enabled: boolean;
    reserveTokens: number;
    keepRecentTokens: number;
    /** Hard upper threshold for context retained between model requests. */
    retainedContextTokens: number;
}
export declare const DEFAULT_COMPACTION_SETTINGS: Readonly<CompactionSettings>;"""

SETTINGS_IMPORT_ANCHOR = 'import { DEFAULT_HTTP_IDLE_TIMEOUT_MS, parseHttpIdleTimeoutMs } from "./http-dispatcher.js";'
SETTINGS_IMPORT_OWNED = (
    SETTINGS_IMPORT_ANCHOR
    + '\nimport { DEFAULT_COMPACTION_SETTINGS } from "./compaction/compaction.js";'
)
SETTINGS_METHODS_STOCK = """    getCompactionEnabled() {
        return this.settings.compaction?.enabled ?? true;
    }
    setCompactionEnabled(enabled) {
        if (!this.globalSettings.compaction) {
            this.globalSettings.compaction = {};
        }
        this.globalSettings.compaction.enabled = enabled;
        this.markModified("compaction", "enabled");
        this.save();
    }
    getCompactionReserveTokens() {
        return this.settings.compaction?.reserveTokens ?? 16384;
    }
    getCompactionKeepRecentTokens() {
        return this.settings.compaction?.keepRecentTokens ?? 20000;
    }
    getCompactionSettings() {
        return {
            enabled: this.getCompactionEnabled(),
            reserveTokens: this.getCompactionReserveTokens(),
            keepRecentTokens: this.getCompactionKeepRecentTokens(),
        };
    }"""
SETTINGS_METHODS_OWNED = """    getCompactionEnabled() {
        return DEFAULT_COMPACTION_SETTINGS.enabled;
    }
    setCompactionEnabled(_enabled) {
        if (!this.globalSettings.compaction) {
            this.globalSettings.compaction = {};
        }
        this.globalSettings.compaction.enabled = DEFAULT_COMPACTION_SETTINGS.enabled;
        this.markModified("compaction", "enabled");
        this.save();
    }
    getCompactionReserveTokens() {
        return this.settings.compaction?.reserveTokens ?? DEFAULT_COMPACTION_SETTINGS.reserveTokens;
    }
    getCompactionKeepRecentTokens() {
        return this.settings.compaction?.keepRecentTokens ?? DEFAULT_COMPACTION_SETTINGS.keepRecentTokens;
    }
    getCompactionSettings() {
        return {
            enabled: this.getCompactionEnabled(),
            reserveTokens: this.getCompactionReserveTokens(),
            keepRecentTokens: this.getCompactionKeepRecentTokens(),
            retainedContextTokens: DEFAULT_COMPACTION_SETTINGS.retainedContextTokens,
        };
    }"""

SETTINGS_DTS_STOCK = """    getCompactionSettings(): {
        enabled: boolean;
        reserveTokens: number;
        keepRecentTokens: number;
    };"""
SETTINGS_DTS_OWNED = """    getCompactionSettings(): {
        enabled: boolean;
        reserveTokens: number;
        keepRecentTokens: number;
        retainedContextTokens: number;
    };"""


def replace_once(path: Path, old: str, new: str) -> None:
    source = path.read_text()
    if source.count(old) != 1:
        raise SystemExit(f"Native compaction owner changed in {path}; expected one anchor")
    path.write_text(source.replace(old, new, 1))


def main(package: Path) -> None:
    compaction = package / "dist/core/compaction/compaction.js"
    compaction_types = package / "dist/core/compaction/compaction.d.ts"
    settings = package / "dist/core/settings-manager.js"
    settings_types = package / "dist/core/settings-manager.d.ts"

    replace_once(compaction, COMPACTION_JS_STOCK, COMPACTION_JS_OWNED)
    replace_once(compaction, SHOULD_COMPACT_STOCK, SHOULD_COMPACT_OWNED)
    replace_once(compaction_types, COMPACTION_DTS_STOCK, COMPACTION_DTS_OWNED)
    replace_once(settings, SETTINGS_IMPORT_ANCHOR, SETTINGS_IMPORT_OWNED)
    replace_once(settings, SETTINGS_METHODS_STOCK, SETTINGS_METHODS_OWNED)
    replace_once(settings_types, SETTINGS_DTS_STOCK, SETTINGS_DTS_OWNED)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
