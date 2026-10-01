"""Run the existing continuous context case without source/conftest substitutes."""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import agent_comms


async def run(root):
    from pytest import MonkeyPatch
    from test_backend_native_lifecycle import native_backend
    from test_native_context_inspection import test_context_manifest_native_acp_and_cli_continuous

    started = time.monotonic()
    root.mkdir(mode=0o700)
    monkey = MonkeyPatch()
    fixture = native_backend.__wrapped__(root, monkey)
    original = None
    receipt = {"python": sys.executable, "core": agent_comms.__file__, "fixture": str(root),
               "public_inputs": 0, "paid_provider_calls": 0}
    try:
        original = await anext(fixture)
        async with asyncio.timeout(90):
            await test_context_manifest_native_acp_and_cli_continuous(original)
        receipt["state"] = "SCOPED_PASS"
    except BaseException as error:
        receipt["state"] = "FAILED_NO_REPLAY"
        receipt["error"] = repr(error)
        raise
    finally:
        await fixture.aclose()
        monkey.undo()
        if original is not None:
            receipt["local_provider_posts"] = original.provider.posts
            requests = root / 'original-provider-requests.json'
            requests.write_text(json.dumps(original.provider.requests))
            receipt['original_provider_requests'] = str(requests)
        receipt["elapsed_seconds"] = time.monotonic() - started
        (root / "terminal-receipt.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--test-support-site", type=Path, required=True)
    parser.add_argument("--toad-driver-dir", type=Path, required=True)
    options = parser.parse_args()
    if "site-packages" not in Path(agent_comms.__file__).parts:
        raise RuntimeError("This acceptance requires the paired installed Core wheel")
    # Only pytest's fixture decorator/MonkeyPatch is borrowed. Import installed
    # Core first and append the support directory; do not process donor .pth files.
    sys.path.append(str(options.test_support_site))
    sys.path.append(str(options.toad_driver_dir))
    os.environ['PATH'] = os.pathsep.join((str(Path(sys.executable).parent), os.environ.get('PATH', os.defpath)))
    asyncio.run(run(options.root))
