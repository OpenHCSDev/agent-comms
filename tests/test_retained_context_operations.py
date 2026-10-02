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


def original_input_consumer_journey(tmp_path, command):
    """Reuse the original ACP reservation fixture; never dispatch its inputs."""
    from test_acp_queue_contract import _owner
    from test_current_input_origin import Client, capture
    from agent_comms.queued_input import QueuedInput
    from agent_comms.store_files import _store_lock

    comms, agent, _, _ = _owner(tmp_path)
    comms.messaging.initialize_private_initial_protocol()
    origin = capture(comms)
    wording = "Never replay UNKNOWN.\nPreserve original λ /source/owned bytes."
    originals = []
    with _store_lock(comms._wire_lock_path):
        for _ in range(2):
            queued, _ = QueuedInput.capture(
                agent.inputs, "beta", text=wording, prompt=wording, echo=True,
                images=(), controller=Client(), origin=origin)
            originals.append(agent.inputs.dispositions.read().lookup(queued.key))
        assert agent.inputs.dispositions.record(
            "neutral-original", seq=None, owner="beta",
            admission=origin.admission.admission_generation, target="beta", text=wording)
    inputs = agent.inputs.dispositions
    original_inputs = inputs.path.read_bytes()
    other = comms.registry.declare(Thread("unaddressed", frozenset(), origin.project))

    def invoke(*args):
        result = command(comms.root, args)
        assert inputs.path.read_bytes() == original_inputs
        assert comms.registry.require("beta").turn_lease is None
        return result

    def reference(ref):
        return f"{ref['seq']}:{ref['message_id']}"

    pins = []
    for row in (originals[0], originals[0], originals[1]):
        code, result = invoke("pin-input-constraint", "beta", "--source", row.key,
                              "--worktree", origin.project)
        assert code == 0
        assert result["source"] == FieldCodec.encode(row.context_provenance())
        pins.append(reference(result["pin"]))
    snapshot = comms.bus.log.retained_context("beta", comms.registry)
    assert FieldCodec.decode(RetainedSegment, FieldCodec.encode(snapshot)) == snapshot
    assert all(row.context_provenance() in snapshot.provenance for row in originals)
    code, inspected = invoke("retained-context", "beta")
    assert code == 0 and inspected["input_supplied"] is False
    assert all(FieldCodec.encode(row.context_provenance()) in inspected["provenance"]
               for row in originals)

    destination = comms.root / "original-input-context.md"
    code, result = invoke("export-retained", "beta", "--output", str(destination))
    assert code == 0 and result["exported_messages"] == 2
    artifact = destination.read_bytes()
    assert artifact.count(wording.encode()) == 2
    assert all(row.key.encode() in artifact for row in originals)
    assert b'"author":"user"' in artifact and b'"author_role":"user"' in artifact
    code, _ = invoke("export-retained", "beta", "--output", str(destination))
    assert code == 1 and destination.read_bytes() == artifact
    before_refusal = (comms.root / "bus.jsonl").read_bytes()
    for recipient, key in ((other.name, originals[0].key), ("beta", "absent"),
                           ("beta", "neutral-original")):
        code, _ = invoke("pin-input-constraint", recipient, "--source", key,
                         "--worktree", origin.project)
        assert code == 1
        assert (comms.root / "bus.jsonl").read_bytes() == before_refusal

    comms.registry.rename("beta", "renamed-beta")

    def renamed(*args):
        result = command(comms.root, args)
        assert inputs.path.read_bytes() == original_inputs
        assert comms.registry.require("renamed-beta").turn_lease is None
        return result

    code, result = renamed("export-retained", "renamed-beta", "--output", str(destination),
                           "--overwrite")
    assert code == 0 and result["exported_messages"] == 2
    replacement = "Keep only the human's corrected original instruction."
    code, _ = renamed("supersede-constraint", "renamed-beta", "--source", pins[1],
                      "--body", replacement, "--worktree", origin.project)
    assert code == 0
    code, result = renamed("export-retained", "renamed-beta", "--output", str(destination),
                           "--overwrite")
    assert code == 0 and result["exported_messages"] == 2
    assert destination.read_text().count(wording) == 1
    assert replacement in destination.read_text()
    code, _ = renamed("drop-constraint", "renamed-beta", "--source", pins[2],
                      "--worktree", origin.project)
    assert code == 0
    code, result = renamed("export-retained", "renamed-beta", "--output", str(destination),
                           "--overwrite")
    assert code == 0 and result["exported_messages"] == 1
    assert wording not in destination.read_text() and replacement in destination.read_text()
    assert snapshot.original_text_source(snapshot.retained.current_authored_sources(
        comms.registry.require("renamed-beta"), comms.registry.snapshot())[0]) == originals[0]
    from agent_comms.comms import Comms
    reopened = Comms(comms.root)
    code, inspected = renamed("retained-context", "renamed-beta")
    assert code == 0
    assert inspected["text"] == reopened.bus.log.retained_context(
        "renamed-beta", reopened.registry).text()
    assert all(inputs.read().lookup(row.key) == row and row.unresolved for row in originals)
    return {"original_inputs": 2, "distinct_equal_wording": True, "pins": 3,
            "refusals": 4, "rename_correction_drop_reopen": True,
            "original_input_bytes_preserved": True, "native_inputs": 0,
            "provider_calls": 0, "public_changes": []}


def test_original_input_pin_query_export_correction_and_drop(tmp_path, capsys):
    def command(root, args):
        code = main(["--root", str(root), *args])
        return code, json.loads(capsys.readouterr().out)

    original_input_consumer_journey(tmp_path, command)
