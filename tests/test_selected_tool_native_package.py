"""The deployed Pi loader must admit the same tool selected by the owner."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_comms.selected_tool_broker import selected_extension


@pytest.mark.parametrize("packaged", [True, False])
def test_actual_pi_loader_admits_packaged_channel_tool_only(tmp_path, packaged):
    configured = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
    if not configured:
        pytest.skip("Prepared native package required")
    package = Path(configured)
    extension = selected_extension(package)
    if not packaged:
        copied = tmp_path / "selected_claimed_write.mjs"
        copied.write_bytes(extension.read_bytes())
        extension = copied
    environment = dict(os.environ, PI_OFFLINE="1", NODE_DISABLE_COMPILE_CACHE="1")
    for key in ("NODE_OPTIONS", "NODE_PATH", "NODE_COMPILE_CACHE"):
        environment.pop(key, None)
    code = r"""
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const [pkg, root, extension] = process.argv.slice(1);
const {DefaultResourceLoader} = await import(
    pathToFileURL(join(pkg,'dist/core/resource-loader.js')));
const {SettingsManager} = await import(pathToFileURL(join(pkg,'dist/core/settings-manager.js')));
const loader = new DefaultResourceLoader({cwd:root, agentDir:root,
    settingsManager:SettingsManager.inMemory(), noExtensions:true, noSkills:true,
    noPromptTemplates:true, noThemes:true, additionalExtensionPaths:[extension]});
await loader.reload();
const result = loader.getExtensions();
console.log(JSON.stringify({tools:result.extensions.flatMap(item=>[...item.tools.keys()]),
    errors:result.errors}));
"""
    result = subprocess.run(
        [
            "node",
            "--no-global-search-paths",
            "--import",
            str(package / "dist/agent-comms-import-fence.mjs"),
            "--input-type=module",
            "--eval",
            code,
            str(package),
            str(tmp_path),
            str(extension),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if packaged:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"tools": ["selected_claimed_write"], "errors": []}
    else:
        assert result.returncode != 0 and "ERR_NATIVE_IMPORT_BOUNDARY" in result.stderr
