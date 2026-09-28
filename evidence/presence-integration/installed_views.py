"""Exercise fresh CLI processes against one owned, disposable catalog."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_comms import AllOfMatch, wire


def main():
    checkout = Path(__file__).resolve().parents[2]
    artifacts = checkout / ".task-artifacts"
    artifacts.mkdir(exist_ok=True)
    receipts = []
    with tempfile.TemporaryDirectory(prefix="view-cli-", dir=artifacts) as directory:
        root = Path(directory)
        env = {**os.environ, "AGENT_COMMS_ROOT": str(root)}
        env.pop("PYTHONPATH", None)

        def invoke(tool, arguments):
            command = [
                sys.executable,
                "-m",
                "agent_comms.cli",
                "--root",
                str(root),
                "invoke",
                "--tool",
                tool,
                "--arguments",
                json.dumps(arguments),
            ]
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
            receipts.append({"tool": tool, "arguments": arguments, "exit_code": result.returncode})
            assert result.returncode == 0, result.stderr + result.stdout
            return json.loads(result.stdout)

        for tag in ("api", "ui"):
            invoke("comms_tags", {"action": "create", "name": tag})
        view = invoke(
            "comms_set_view",
            {
                "name": "work",
                "kind": "activity",
                "match": "all_of",
                "tags": "ui,api",
            },
        )
        assert view["predicate"] == {"match": "all_of", "tags": ["api", "ui"]}
        assert view["created_at"] > 0
        assert invoke("comms_channels", {})["views"] == [view]
        stored = json.loads((root / "saved_views.json").read_text())
        assert stored == {
            "views": {"work": {key: value for key, value in view.items() if key != "name"}}
        }
        invoke("comms_tags", {"action": "rename", "name": "ui", "new_name": "docs"})
        reopened = wire(root)
        renamed = reopened.saved_views()["work"]
        assert renamed.predicate.match is AllOfMatch
        assert renamed.predicate.tags == {"api", "docs"}
        assert renamed.created_at == view["created_at"]
        assert renamed.predicate.matches(frozenset({"api", "docs"}))
        assert not renamed.predicate.matches(frozenset({"api"}))
        invoke("comms_delete_view", {"name": "work"})
        assert invoke("comms_channels", {})["views"] == []
        assert reopened.bus.latest_sequence() == 0
    print(
        json.dumps(
            {
                "processes": receipts,
                "saved_format_preserved": True,
                "typed_filter_after_reopen": True,
                "no_bus_messages": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
