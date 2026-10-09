"""Real retained owner/native/Codex summary with representative private history."""

from agent_comms.owner_launch import RestartEnvironment
from agent_comms.selected_session import SavedSelectedSession
import asyncio
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from agent_comms.agent_events import CompactionSummaryProgress
from agent_comms.child_process import AttachedChild, ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.input_disposition import InputDispositions
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.native_session_prepare import NativeSessionPreparation
from agent_comms.owner_compaction_adaptive import maybe_compact_owner_turn
from agent_comms.native_input_owner import RegistryOwner
from agent_comms.registration import Registration
from agent_comms.selected_pi_summary_rpc import SelectedChildUnknown
from agent_comms.store_files import _store_lock
from agent_comms.threads import Thread
from codex_loopback_provider import CodexLoopbackProvider, local_codex_key
from retained_native_fixture import retained_native_host

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
SAVED = os.environ.get("COMPACTION_REPRESENTATIVE_SAVED_COPY")
pytestmark = pytest.mark.skipif(
    not PACKAGE or not SAVED, reason="Owned native package and saved copy required"
)


@pytest.mark.parametrize("overrun", [False, True], ids=["reasoning", "retained-overrun"])
async def test_retained_summary_accounting_and_original_custody(tmp_path, monkeypatch, overrun):
    package = Path(PACKAGE).resolve()
    source = Path(SAVED)
    file = tmp_path / "saved.jsonl"
    shutil.copyfile(source, file)
    file.chmod(0o600)
    before = hashlib.sha256(file.read_bytes()).hexdigest()
    original = "ISOLATED_ACCOUNTING_ACCEPTANCE"  # Never the live failed input.
    monkeypatch.setenv("PI_COMPACTION_TEST_PACKAGE", str(package))
    monkeypatch.setenv("PR95_OWNER_FIXTURE_ROOT", str(tmp_path))
    monkeypatch.setenv("PR95_OWNER_SAVED_SESSION", str(file))
    monkeypatch.setenv("PR95_RUNTIME_API_KEY", local_codex_key())
    monkeypatch.setenv(
        "PR95_NATIVE_SETTINGS",
        json.dumps(
            {
                "compaction": {"enabled": True, "reserveTokens": 16384, "keepRecentTokens": 20000},
                "retry": {"enabled": False},
                "transport": "sse",
            }
        ),
    )
    monkeypatch.setenv("AGENT_COMMS_SESSION_INDEX_DIR", str(tmp_path / "indexes"))
    monkeypatch.setenv("AGENT_COMMS_ROOT", str(tmp_path))
    root_id = Comms(tmp_path).messaging.initialize_private_initial_protocol()
    monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID", root_id)
    monkeypatch.setenv("AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE", str(package))
    with CodexLoopbackProvider(retained=20000 if overrun else 20) as provider:
        latency_receipt = os.environ.get("COMPACTION_LATENCY_RECEIPT")
        starts, finishes = {}, {}
        if latency_receipt and not overrun:
            def response(request, number):
                starts[number] = time.monotonic()
                time.sleep(0.8 if number in (1, 5) else 0.02)
                return provider.text, provider.retained, provider.reasoning

            def completed(request, number, index):
                if index == (len(provider.text) + provider.chunk_characters - 1) // provider.chunk_characters + 2:
                    finishes[number] = time.monotonic()

            provider.response_factory = response
            provider.after_chunk = completed
        (tmp_path / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "local-owner": {
                            "api": "openai-codex-responses",
                            "apiKey": local_codex_key(),
                            "baseUrl": provider.base_url,
                            "models": [
                                {
                                    "id": "selected",
                                    "name": "Offline Codex contract",
                                    "contextWindow": 272000,
                                    "maxTokens": 128000,
                                    "reasoning": True,
                                }
                            ],
                        }
                    }
                }
            )
        )
        child = await AttachedChild.start(
            ("node", str(Path(__file__).parents[1] / "stack/test-native-selected-owner-host.mjs")),
            env=dict(os.environ),
        )
        persistent = None
        try:
            ready = json.loads(await asyncio.wait_for(child.stderr.readline(), 15))
            before = hashlib.sha256(file.read_bytes()).hexdigest()
            persistent = retained_native_host(
                child,
                NativePiRpcLaunch(("node",), tmp_path, {}, SavedSelectedSession(tmp_path, identity=NativeSessionIdentity(ready["sessionId"], str(file))), package, configuration=RestartEnvironment.inherit({})),
                NativeSessionIdentity(ready["sessionId"], str(file)),
            )
            (tmp_path / "settings.json").write_text(os.environ["PR95_NATIVE_SETTINGS"])
            prepared = await NativeSessionPreparation.open(
                persistent, "pi",
                ["--provider", "local-owner", "--model", "selected", "--thinking", "low",
                 "--offline", "--no-extensions", "--no-skills", "--no-context-files",
                 "--no-prompt-templates", "--no-tools"],
                worktree=str(tmp_path), environment=dict(os.environ), session_file=str(file),
            )
            before = hashlib.sha256(file.read_bytes()).hexdigest()
            registry = Registration(tmp_path / "registry.json")
            registry.register(
                Thread(
                    "owner",
                    frozenset(),
                    str(tmp_path),
                    process_identity=ProcessIdentity.capture(os.getpid()),
                    session_file=str(file),
                    model=ready["model"],
                )
            )
            owner, generation = registry.live_owner_with_generation("owner")
            owner, _ = registry.lease_live_turn_with_generation(
                owner, "acceptance", expected_owner_generation=generation
            )
            inputs = InputDispositions(tmp_path / InputDispositions.filename)
            inputs.record(
                "acp:acceptance",
                seq=None,
                owner="owner",
                target="owner",
                admission=owner.active_turn.admission_generation,
                text=original,
            )
            admitted, observed = [], []

            async def observe(event):
                observed.append(event)

            async def compact():
                return await maybe_compact_owner_turn(
                    registry,
                    RegistryOwner.capture_local(registry.snapshot(), "owner"),
                    prepared,
                    ("acp:acceptance",),
                    persistent,
                    input_text=original,
                    on_admission=admitted.append,
                    on_event=observe,
                )

            baseline = os.environ.get("COMPACTION_EXPECT_BASELINE_REFUSAL") == "1"
            if overrun or baseline:
                with pytest.raises(SelectedChildUnknown, match="native plan output token budget"):
                    await asyncio.wait_for(compact(), 45)
                assert not admitted
                assert hashlib.sha256(file.read_bytes()).hexdigest() == before
                assert inputs.read().lookup("acp:acceptance").accepts_reservation
                rows = CompactionJournal(
                    tmp_path / "compaction-commits.sqlite3"
                ).summaries.blocking(str(file))
                assert len(rows) == 1 and rows[0].state.declared_name == "unknown"
            else:
                assert await asyncio.wait_for(compact(), 45)
                assert len(admitted) == 1
                assert any(
                    isinstance(event, CompactionSummaryProgress) and event.text
                    for event in observed
                )
                journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
                rows = journal.summaries.blocking(str(file))
                assert len(rows) == 1 and rows[0].state.declared_name == "linked"
                with _store_lock(tmp_path / "wire"):
                    token = admitted[0]
                    assert token.consume_bound_original(
                        wire_root=tmp_path,
                        session_file=str(file),
                        identity=token._identity,
                        native_id="a" * 32,
                        sent_text=original,
                        dispositions=inputs,
                    )
                    assert not token.consume_bound_original(
                        wire_root=tmp_path,
                        session_file=str(file),
                        identity=token._identity,
                        native_id="b" * 32,
                        sent_text=original,
                        dispositions=inputs,
                    )
            assert not provider.failures, provider.failures
            assert 1 <= len(provider.requests) <= 16
            assert all(original not in json.dumps(request) for request in provider.requests)
            if latency_receipt and not overrun:
                assert len(provider.requests) == 9
                assert len(starts) == len(finishes) == 9
                rolling = os.environ.get("COMPACTION_LATENCY_BASELINE") != "1"
                assert (starts[5] < finishes[1]) is rolling
                first = starts[1]
                Path(latency_receipt).write_text(json.dumps({
                    "rolling": rolling,
                    "provider_posts": len(provider.requests),
                    "history_map_seconds": max(finishes[n] for n in range(1, 6)) - first,
                    "provider_workflow_seconds": max(finishes.values()) - first,
                    "spans": [{"call": n, "start": starts[n] - first, "finish": finishes[n] - first}
                              for n in sorted(starts)],
                    "original_bindings": len(admitted),
                    "journal_state": rows[0].state.declared_name,
                }, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "saved_bytes": source.stat().st_size,
                        "provider_posts": len(provider.requests),
                        "retained_tokens": provider.retained,
                        "reasoning_tokens": provider.reasoning,
                        "overrun": overrun,
                        "baseline_refusal": baseline,
                        "original_bindings": len(admitted),
                    }
                )
            )
        finally:
            if persistent is not None:
                await persistent.close()
            else:
                await child.close()
