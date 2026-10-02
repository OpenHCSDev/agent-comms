#!/usr/bin/env python3
"""Keep canonical effective settings while isolating tracked native retries."""

# ruff: noqa: E501 - exact pinned JavaScript anchors
import hashlib
import sys
from pathlib import Path


def patch(path: Path, digest: str, replacements: tuple[tuple[str, str], ...]) -> None:
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit(f"Adaptive settings require pinned source: {path.name}")
    source = path.read_text()
    for old, new in replacements:
        if source.count(old) != 1:
            raise SystemExit(f"Adaptive settings anchor changed: {old[:80]}")
        source = source.replace(old, new, 1)
    path.write_text(source)


def main(package: Path) -> None:
    patch(
        package / "dist/core/agent-session-services.js",
        "4af410d793207f0269cf442a799b0f83933b69d728d166e49a3a6134ff7108a6",
        (
            (
                "const settingsManager = options.settingsManager ?? SettingsManager.create(cwd, agentDir);",
                """const settingsManager = options.settingsManager ?? SettingsManager.create(cwd, modelDir);
    // SettingsManager owns canonical/project trust and tracked retry policy.""",
            ),
        ),
    )
    patch(
        package / "dist/core/settings-manager.js",
        "ee4f52d1dd4f1c18d5d814be4ba260ddf7fe40b7b70c2f0732a30a8b287111ad",
        (
            (
                "        return this.settings.retry?.enabled ?? true;",
                "        return process.env.AGENT_COMMS_NATIVE_CONFIG_DIR ? false : (this.settings.retry?.enabled ?? true);",
            ),
            (
                "            maxRetries: this.settings.retry?.maxRetries ?? 3,",
                "            maxRetries: process.env.AGENT_COMMS_NATIVE_CONFIG_DIR ? 0 : (this.settings.retry?.maxRetries ?? 3),",
            ),
            (
                "            maxRetries: this.settings.retry?.provider?.maxRetries,",
                "            maxRetries: process.env.AGENT_COMMS_NATIVE_CONFIG_DIR ? 0 : this.settings.retry?.provider?.maxRetries,",
            ),
        ),
    )
    patch(
        package / "dist/core/agent-session.js",
        "1100105722d5f4d5635bc3103a73209e0d3b23759467bd1f1ee2b108be4ba2b0",
        (
            (
                "if (!this._nativeRunHadTrackedInput || !this.model || this.model.contextWindow <= 0)",
                "if (process.env.AGENT_COMMS_NATIVE_CONFIG_DIR || !this._nativeRunHadTrackedInput || !this.model || this.model.contextWindow <= 0)",
            ),
            (
                "    async _checkCompaction(assistantMessage, skipAbortedCheck = true) {",
                """    async _checkCompaction(assistantMessage, skipAbortedCheck = true) {
        // The private owner journals selected summaries before input; never
        // introduce an unjournaled overflow retry or automatic summary here.
        if (process.env.AGENT_COMMS_NATIVE_CONFIG_DIR) return false;""",
            ),
        ),
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
