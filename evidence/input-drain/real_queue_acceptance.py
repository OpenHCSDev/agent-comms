"""Actual configured-provider queue acceptance. Each run requires a fresh project.

Run with the candidate installed Python, passing --run and --package. This uses
ordinary ACP input and the normal native launcher; it never modifies history or
retries an uncertain input. Only the new owned project's threshold is lowered.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from agent_comms.acp import CommsAgent
from agent_comms.input_disposition import InputDispositions
from agent_comms.operations import Comms
from agent_comms.selected_pi_route import read_selected_compaction_decision


def journal(root, table):
    path = root / "compaction-commits.sqlite3"
    if not path.exists():
        return []
    assert table in ("selected_summary_attempts", "operations")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


def native_entries(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def answer_text(entry):
    return "".join(
        part.get("text", "")
        for part in entry["message"].get("content", [])
        if part.get("type") == "text"
    )


async def run(args):
    project = args.workspace / args.run
    project.mkdir(parents=True, exist_ok=False)
    (project / ".pi").mkdir()
    (project / ".pi/settings.json").write_text(
        json.dumps({"compaction": {
            "enabled": True, "reserveTokens": 263808, "keepRecentTokens": 512
        }}) + "\n"
    )
    root = Path(tempfile.mkdtemp(prefix="ac-pr95-queue-", dir="/var/tmp"))
    receipt = project / "result.json"
    report = {
        "project": str(project), "root": str(root), "started_at": time.time(),
        "package": str(args.package), "launcher": str(args.launcher),
        "verified_success": False, "turns": [],
    }

    def save():
        receipt.write_text(json.dumps(report, indent=2, default=str) + "\n")

    class Client:
        async def session_update(self, **kwargs):
            with (project / "updates.jsonl").open("a") as stream:
                stream.write(json.dumps(kwargs, default=lambda x: x.model_dump()) + "\n")

    comms = Comms(root)
    wire_id = comms.initialize_private_initial_protocol()
    comms.initialize_private_claim_protocol()
    os.environ.update({
        "AGENT_COMMS_ROOT": str(root),
        "AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID": wire_id,
        "AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE": str(args.package),
    })
    agent = CommsAgent(
        comms, agent_bin=str(args.launcher),
        agent_args=["--provider", "openai-codex", "--model", "gpt-6-sol",
                    "--thinking", "medium", "--approve", "--no-tools",
                    "--no-extensions", "--no-skills", "--no-prompt-templates"],
        runtime_enabled=True, auto_wake=False,
        private_nk_native_package=args.package, private_nk_wire_root_id=wire_id,
    )
    agent.on_connect(Client())
    save()
    try:
        session = await agent.new_session(cwd=str(project), mcp_servers=[])
        name = session.session_id
        report["session_id"] = name

        async def prompt(text):
            started = time.time()
            response = await agent.prompt(name, [{"type": "text", "text": text}])
            report["turns"].append({
                "response": response.model_dump(), "seconds": time.time() - started
            })
            save()
            assert not agent.turns.emitted_errors, agent.turns.emitted_errors
            return response

        background = "\n".join(
            f"Archive record {i}: routine fixture observation, no change to project facts, "
            "no outstanding task, no action required." for i in range(420)
        )
        recent = "\n".join(
            f"Recent observation {i}: acknowledged fixture entry; no new requirement."
            for i in range(55)
        )
        await prompt(
            "Remember these exact project facts across future summaries: lighthouse code "
            "ORCHID-7301; delivery window Thursday 14:30; rejected option cobalt; chosen "
            "material cedar. Preserve all four facts. Reply RECORDED and list them. "
            "The following background is disposable test context:\n" + background
        )
        owner = comms.registry.require(name)
        native_file = Path(owner.session_file)
        report["session_file"] = str(native_file)
        info = comms.agent_info_of(name)
        decision = await read_selected_compaction_decision(
            agent.turns.persistent_backends[name], session_file=owner.session_file,
            expected_launcher=agent.turns.agent_bin, provider="openai-codex",
            model_id="gpt-6-sol", context_tokens=info.context_used,
            context_window=info.context_size,
        )
        report["effective_reserve_tokens"] = decision.reserve_tokens
        assert decision.reserve_tokens == 263808 and decision.trigger, decision
        await prompt("Keep our original four facts; read this context and reply ACK only:\n" + recent)
        before = {row["operation_id"] for row in journal(root, "selected_summary_attempts")}
        original_text = "This is the queue original. Preserve the four facts. Reply QUEUE_ORIGINAL_OK only."
        original = asyncio.create_task(prompt(original_text))
        try:
            async with asyncio.timeout(20):
                while True:
                    reserved = [
                        row for row in journal(root, "selected_summary_attempts")
                        if row["status"] == "reserved" and row["operation_id"] not in before
                    ]
                    if reserved:
                        break
                    if original.done():
                        await original
                        raise AssertionError("Original completed without the expected selected summary")
                    await asyncio.sleep(0.02)
            report["selected_operation"] = reserved[0]["operation_id"]
            response = await agent.prompt(name, [{
                "type": "text",
                "text": "This is a new queued follow-up, not a correction. After the original "
                        "response, reply QUEUE_FOLLOWUP_OK and list all four original project facts.",
            }], field_meta={"agentComms": {"delivery": "queue"}})
            acceptance = response.field_meta["agentComms"]["inputDisposition"]
            report["queue_acceptance"] = acceptance
            assert acceptance["status"] == "accepted_not_started", acceptance
            queued_key = "acp:" + acceptance["inputId"]
            dispositions = InputDispositions(root)
            accepted_row = dispositions.get(queued_key)
            assert accepted_row["status"] == "unknown" and accepted_row["native_id"] is None
            report["queue_before_start"] = accepted_row
            save()
            await asyncio.wait_for(original, 150)
        finally:
            if not original.done():
                original.cancel()
                await asyncio.gather(original, return_exceptions=True)

        async with asyncio.timeout(60):
            while True:
                entries = native_entries(native_file)
                answers = [answer_text(e) for e in entries if e.get("type") == "message"
                           and e["message"].get("role") == "assistant"]
                followup = [text for text in answers if text.startswith("QUEUE_FOLLOWUP_OK")]
                if followup and comms.registry.require(name).active_turn is None:
                    break
                assert not agent.turns.emitted_errors, agent.turns.emitted_errors
                await asyncio.sleep(0.1)
        assert len(followup) == 1, followup
        assert all(fact in followup[0] for fact in ("ORCHID-7301", "Thursday", "14:30", "cobalt", "cedar")), followup
        rows = dispositions._read()
        original_row = next(row for row in rows.values() if row["source_text"] == original_text)
        queued_row = rows[queued_key]
        positions = []
        for row in (original_row, queued_row):
            assert row["status"] == "started", row
            starts = [i for i, entry in enumerate(entries) if entry.get("type") == "message"
                      and entry["message"].get("inputId") == row["native_id"]]
            assert len(starts) == 1, starts
            positions.append(starts[0])
        attempts = journal(root, "selected_summary_attempts")
        selected = next(row for row in attempts if row["operation_id"] == report["selected_operation"])
        assert selected["status"] == "linked", selected
        operations = journal(root, "operations")
        committed = next(row for row in operations if row["commit_id"] == selected["commit_id"])
        assert committed["status"] == "committed", committed
        compactions = [i for i, entry in enumerate(entries) if entry.get("type") == "compaction"]
        assert compactions and compactions[-1] < positions[0] < positions[1]
        assert sum(text.strip() == "QUEUE_ORIGINAL_OK" for text in answers) == 1
        assert not [row for row in rows.values() if row["status"] == "unknown"]
        assert not [row for row in attempts if row["status"] in ("unknown", "reserved")]
        assert not agent.turns.emitted_errors, agent.turns.emitted_errors
        report.update({
            "verified_success": True, "original_input": original_row,
            "queued_input": queued_row, "native_start_positions": positions,
            "compaction_positions": compactions, "assistant_answers": answers,
            "selected_summaries": attempts, "operations": operations,
        })
    except BaseException as error:
        report["error"] = repr(error)
        raise
    finally:
        await agent.shutdown()
        report["finished_at"] = time.time()
        save()
        print(json.dumps({"receipt": str(receipt), "verified_success": report["verified_success"],
                          "error": report.get("error")}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
