"""One original human-source workflow through the public CLI and file effects."""

import json

from agent_comms.cli import main
from agent_comms.field_codec import FieldCodec
from agent_comms.retained_context import RetainedSegment
from agent_comms.threads import Thread


def test_original_human_pin_revision_drop_and_atomic_export(comms, capsys):
    comms.messaging.initialize_private_initial_protocol()
    owner = comms.registry.declare(Thread("recipient", frozenset(), str(comms.root)))
    other = comms.registry.declare(Thread("unaddressed", frozenset(), str(comms.root)))
    wording = "Never replay UNKNOWN.\nPreserve exact λ /source/owned bytes."
    original = comms.messaging.send_user_message(owner.name, wording, worktree=owner.worktree)
    original_prefix = (comms.root / "bus.jsonl").read_bytes()

    def command(*args):
        code = main(["--root", str(comms.root), *args])
        return code, json.loads(capsys.readouterr().out)

    def reference(ref):
        return f"{ref['seq']}:{ref['message_id']}"

    source = reference(FieldCodec.encode(original.reference))
    code, pinned = command("pin-constraint", owner.name, "--source", source,
                           "--worktree", owner.worktree)
    assert code == 0
    pin = reference(pinned["pin"])
    snapshot = comms.bus.log.retained_context(owner.name, comms.registry)
    assert FieldCodec.decode(RetainedSegment, FieldCodec.encode(snapshot)) == snapshot
    assert snapshot.text().count(wording.replace("\n", "\\n")) == 1
    assert comms.registry.require(owner.name).turn_lease is None

    destination = comms.root / "AGENTS-export.md"
    code, exported = command("export-retained", owner.name, "--output", str(destination))
    assert code == 0 and exported["exported_messages"] == 1
    artifact = destination.read_bytes()
    assert artifact.count(wording.encode()) == 1 and source.split(":")[1].encode() in artifact
    assert b'"author":"user"' in artifact and b'"author_role":"user"' in artifact
    assert b'"human_constraint_pin"' in artifact
    code, refused = command("export-retained", owner.name, "--output", str(destination))
    assert code == 1 and destination.read_bytes() == artifact
    assert not list(comms.root.glob(".AGENTS-export.md.*.tmp"))
    before_refusal = (comms.root / "bus.jsonl").read_bytes()
    code, refused = command("pin-constraint", other.name, "--source", source)
    assert code == 1 and "did not receive" in refused["error"]
    assert (comms.root / "bus.jsonl").read_bytes() == before_refusal
    code, empty = command("export-retained", other.name, "--output", str(comms.root / "other.md"))
    assert code == 0 and empty["exported_messages"] == 0
    assert wording.encode() not in (comms.root / "other.md").read_bytes()

    replacement = "Keep only the human's corrected /source/new instruction."
    code, revised = command("supersede-constraint", owner.name, "--source", pin,
                            "--body", replacement, "--worktree", owner.worktree)
    assert code == 0
    code, exported = command("export-retained", owner.name, "--output", str(destination), "--overwrite")
    assert code == 0 and exported["exported_messages"] == 1
    assert replacement in destination.read_text() and wording not in destination.read_text()
    assert snapshot.text().count(wording.replace("\n", "\\n")) == 1
    current = comms.bus.log.retained_context(owner.name, comms.registry)
    assert wording.replace("\n", "\\n") in current.text() and replacement in current.text()
    code, dropped = command("drop-constraint", owner.name, "--source", pin,
                            "--worktree", owner.worktree)
    assert code == 0
    code, exported = command("export-retained", owner.name, "--output", str(destination), "--overwrite")
    assert code == 0 and exported["exported_messages"] == 0
    assert replacement not in destination.read_text()
    assert (comms.root / "bus.jsonl").read_bytes().startswith(original_prefix)
    assert comms.registry.require(owner.name).turn_lease is None

    # No semantic sidecar is needed for a new Comms instance to read the result.
    from agent_comms.comms import Comms

    reopened = Comms(comms.root)
    restored = reopened.bus.log.retained_context(owner.name, reopened.registry)
    assert restored.text() == comms.bus.log.retained_context(owner.name, comms.registry).text()
