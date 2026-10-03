"""Saved identity selection and strict history loading have distinct owners."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_comms.native_session_reopen import NativeReopenError, NativeSessionIdentity

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not PACKAGE, reason="Disposable Linux canonical native fixture"
)


@pytest.fixture
def saved(tmp_path):
    assert PACKAGE
    code = """
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
const managerURL = pathToFileURL(join(process.argv[1], 'dist/core/session-manager.js'));
const {SessionManager} = await import(managerURL);
const manager = SessionManager.create(process.argv[2], join(process.argv[2], 'sessions'));
manager.appendMessage({role:'user', content:'task', timestamp:1});
manager.appendMessage({role:'assistant',content:[{type:'text',text:'answer'}],
  provider:'fixture',model:'fixture',api:'fixture',stopReason:'stop',timestamp:2});
console.log(JSON.stringify({id:manager.getSessionId(),file:manager.getSessionFile()}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", code, PACKAGE, str(tmp_path)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    item = json.loads(result.stdout)
    assert item["file"] and item["id"]
    return Path(PACKAGE), Path(item["file"]), item["id"]


def test_saved_identity_preserves_bytes_and_strips_preload(saved, tmp_path, monkeypatch):
    package, file, identity = saved
    before = file.read_bytes()
    marker = tmp_path / "ambient-marker"
    preload = tmp_path / "ambient.mjs"
    preload.write_text(
        "import {writeFileSync} from 'node:fs';"
        f"writeFileSync({json.dumps(str(marker))},'unsafe');"
    )
    monkeypatch.setenv("NODE_OPTIONS", f"--import={preload.as_uri()}")
    observed = NativeSessionIdentity.read(package, str(file))
    assert observed == NativeSessionIdentity(identity, str(file))
    assert file.read_bytes() == before and not marker.exists()
    with pytest.raises(NativeReopenError, match="identity changed"):
        observed.require_same_session(NativeSessionIdentity("wrong", str(file)))


@pytest.mark.parametrize("mutation", ["legacy", "missing", "symlink"])
def test_invalid_disk_never_repaired(saved, tmp_path, mutation):
    package, file, identity = saved
    rows = file.read_text().splitlines()
    if mutation == "legacy":
        header = json.loads(rows[0])
        header["version"] = 2
        file.write_text("\n".join([json.dumps(header), *rows[1:]]) + "\n")
    elif mutation == "missing":
        file.unlink()
    else:
        alias = tmp_path / "session-link"
        alias.symlink_to(file)
        file = alias
    before = file.read_bytes() if file.exists() else None
    with pytest.raises(NativeReopenError):
        NativeSessionIdentity.read(package, str(file))
    assert (file.read_bytes() if file.exists() else None) == before


@pytest.mark.parametrize("mutation", ["tail", "ancestry"])
def test_native_loader_rejects_invalid_history_without_repair(saved, mutation):
    package, file, identity = saved
    if mutation == "tail":
        file.write_bytes(file.read_bytes().rstrip(b"\n"))
    else:
        rows = file.read_text().splitlines()
        entry = json.loads(rows[1])
        entry["parentId"] = "forged"
        file.write_text("\n".join([rows[0], json.dumps(entry)]) + "\n")
    before = file.read_bytes()
    # The original header is still the same. It cannot attest valid history;
    # the real loader must refuse before constructing an agent or provider.
    assert NativeSessionIdentity.read(package, str(file)) == NativeSessionIdentity(identity, str(file))
    result = subprocess.run([
        "node", "--no-global-search-paths", "--import", str(package / "dist/agent-comms-import-fence.mjs"),
        "--input-type=module", "-e",
        "import {pathToFileURL} from 'node:url'; import {join} from 'node:path';"
        "const {SessionManager}=await import(pathToFileURL(join(process.argv[1], 'dist/core/session-manager.js')));"
        "SessionManager.open(process.argv[2]);",
        str(package), str(file),
    ], capture_output=True, timeout=10)
    assert result.returncode != 0
    assert file.read_bytes() == before
