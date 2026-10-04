"""One configured retained native turn through the original selected runtime.

Uses the existing CurrentTypedCapture and PrivateInputs SDK fork operation.
This is an acceptance driver, not a runtime source or a provider substitute.
No public inputs, retries, source overlays or copied credentials.
"""

import asyncio
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import sys
import time
import traceback

import agent_comms
from agent_comms.assignment_states import CompletedAssignment
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import Comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.coordination_cohort import accept_delivery_cohort
from agent_comms.coordinator import Coordination
from agent_comms.coordinated_runtime import SelectedExecution
from agent_comms.field_codec import FieldCodec
from agent_comms.native_entries import NativeEntry
from agent_comms.native_fork import ForkSessionRequest
from agent_comms.native_package import verify_native_package
from agent_comms.native_runtime_input import NativeRuntimeInput
from agent_comms.threads import Thread

TOOLS = Path('/home/ts/wt/comms-cleanup-live-integration-20260929/tools/cutover')
sys.path.insert(0, str(TOOLS))
from original_owner_capture import CurrentTypedCapture

CHECKOUT = Path('/home/ts/wt/comms-goal-ledger-schema-carry-20261002')
ROOT = Path('/home/ts/.cache/agent-scratch/m549')
PACKAGE = Path('/home/ts/wt/comms-task-aware-native-bundle-20261002/stack/.pi-native-960296fdddafb01c/node_modules/@earendil-works/pi-coding-agent')
PUBLIC = Path('/var/tmp/agent-comms-live-20260927-wzjtqhza')
DONOR = Path('/home/ts/.cache/agent-scratch/mendel-configured-sdk542-20261002/native-forks/2026-10-02T19-00-31-323Z_01a0fdfd-4de2-707c-bc2e-b2209f8cdbd8.jsonl')
TOKEN = 'TRACKED_TERMINAL549_CONFIGURED_ONCE'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


async def run():
    started = time.monotonic()
    ROOT.mkdir(mode=0o700)  # One fresh run; an existing run cannot be replayed.
    receipt = {'state': 'PREPARING', 'python': sys.executable,
        'core': agent_comms.__file__, 'public_inputs': 0, 'retries': 0,
        'root': str(ROOT), 'scope': 'configured saved-fork selected native turn; no UI or public-wave latency claim'}
    def save():
        (ROOT / 'receipt.json').write_text(json.dumps(receipt, indent=2))
    save()
    try:
        installed = Path(agent_comms.__file__).parent
        sources = {}
        for file in sorted((CHECKOUT / 'src/agent_comms').rglob('*')):
            if not file.is_file() or '__pycache__' in file.parts:
                continue
            relative = file.relative_to(CHECKOUT / 'src/agent_comms')
            actual = installed / relative
            assert actual.is_file(), str(relative)
            assert digest(file) == digest(actual), str(relative)
            sources[str(relative)] = digest(actual)
        (ROOT / 'installed-source.json').write_text(json.dumps(sources, indent=2))
        receipt.update(installed_files=len(sources), sdk=metadata.version('agent-client-protocol'),
            direct_url=json.loads(metadata.distribution('agent-comms').read_text('direct_url.json')),
            native_package=str(PACKAGE))
        assert receipt['sdk'] == '0.12.1'
        verify_native_package(PACKAGE)
        receipt['native_manifest_sha256'] = digest(installed / '_native/pi-native.sha256')
        original_python = Path('/home/ts/.local/bin/agent-comms').resolve().parent / 'python'
        captured = CurrentTypedCapture(PUBLIC, original_python).read('nra-architecture')
        source = captured.require_current()
        original_settings = (source.model, source.thinking_level, source.worktree,
                             source.session_file, source.process_identity)
        proof = Path(str(DONOR) + '.input-proof')
        before = {str(file): digest(file) for file in (DONOR, proof)}
        assert before[str(DONOR)] == 'c1137f89f1a79704c834bad0f84369cd0fb38d100ee3e6e6924ad2b817f09bae'
        assert before[str(proof)] == 'b19f73c9f8fcb434e3ba318ba6ce7c3ddc762f9fcf15ac99e9864b192d4eb9b5'
        receipt.update(model=source.model, thinking=source.thinking_level.declared_name,
            original_selection=FieldCodec.encode(captured.selection),
            source_donor=str(DONOR), source_bytes=DONOR.stat().st_size,
            original_hashes=before, original_python=str(original_python))
        project = ROOT / 'project'
        project.mkdir(mode=0o700)
        wire = ROOT / 'wire'
        wire.mkdir(mode=0o700)
        service = Comms(wire, private_initial_writes=True)
        environment = dict(captured.retained.environment)
        fork = await CompactionJournal(wire / 'compaction-commits.sqlite3').private_inputs.fork(
            ForkSessionRequest(str(PACKAGE), str(DONOR), str(project), str(ROOT / 'native-forks')),
            cwd=project, env=environment)
        selected = Path(fork.session_file)
        assert selected.is_relative_to(ROOT)
        baseline = selected.read_bytes()
        receipt['native_fork'] = FieldCodec.encode(fork)
        receipt['fork_original_prefix_sha256'] = hashlib.sha256(baseline).hexdigest()
        sender = Thread('terminal549-sender', frozenset(), str(project),
            process_identity=ProcessIdentity.capture(os.getpid()))
        owner = Thread('terminal549-owner', frozenset(), str(project),
            process_identity=ProcessIdentity.capture(os.getpid()), model=source.model,
            thinking_level=source.thinking_level, session_file=fork.session_file,
            task='Only this bounded acceptance. Never resume inherited goals or tasks; change only the private marker requested now.')
        for thread in (sender, owner):
            service.registry.declare(thread)
        root_id = service.messaging.initialize_private_initial_protocol()
        marker = project / 'terminal549-marker.txt'
        prompt = ('Bounded acceptance only. Do not resume inherited work, contact peers, '
            'or change any other file. Use the native write tool once to create '
            f'{marker} containing exactly {TOKEN}. Then reply exactly {TOKEN}.')
        original = service.messaging.send_initial_cohort(sender.name, owner.name, prompt)
        cohort = service.bus.log.read_delivery_cohort(root_id, original.seq)
        with Coordination(wire / 'coordination.sqlite3') as store:
            for recipient in cohort.audience.recipients:
                store.participants.register(recipient.recipient_lookup,
                    recipient.canonical_thread, recipient.canonical_thread, committed=True)
            accepted = accept_delivery_cohort(service.bus, root_id, original.seq, store)
            assert accepted.value.member_count == 1
        for name in ('PYTHONPATH', 'AGENT_COMMS_THREAD', 'AGENT_COMMS_STARTUP_INPUT_KEY',
            'PI_PROMPT', 'PI_PARENT_ID', 'PI_TASK', 'PI_AGENT_ID', 'PI_AGENT_TAGS',
            'AGENT_COMMS_TAGS', 'AGENT_COMMS_SELECTED_TOOL_SOCKET', 'AGENT_COMMS_SELECTED_TOOL_TOKEN'):
            environment.pop(name, None)
        environment.update(AGENT_COMMS_ROOT=str(wire),
            AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
            AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(PACKAGE),
            PI_COMPACTION_TEST_PACKAGE=str(PACKAGE),
            AGENT_COMMS_AGENT_BIN=str(Path(sys.executable).parent / 'pi-comms-native'))
        os.environ.clear()
        os.environ.update(environment)
        receipt.update(state='ONE_PRIVATE_ORIGINAL_ABOUT_TO_EXECUTE_NO_RETRY',
            original_message_id=original.message_id, original_sequence=original.seq,
            marker=str(marker))
        save()
        began = time.monotonic()
        # The existing native/lease owners retain their normal deadlines and
        # cancellation cleanup. There is no extra whole-turn timeout here.
        result = await SelectedExecution(root=wire, wire_root_id=root_id,
            owner_name=owner.name, native_package=PACKAGE, session_file=selected).run()
        receipt['selected_execution_seconds'] = time.monotonic() - began
        assert result is not None and result.disposition is CompletedAssignment
        receipt['result'] = FieldCodec.encode(result)
        assert result.publications
        assert marker.read_text() == TOKEN
        assert len(service.bus.log.claim_projection()) == 0
        current = service.registry.require(owner.name)
        current.require_idle()
        with Coordination(wire / 'coordination.sqlite3') as store, store.session.read():
            rows = NativeRuntimeInput.select(store.session._connection)
            assert len(rows) == 1 and rows[0].input_id == result.input_id
            assert rows[0].session_id == fork.session_id
            receipt['native_input'] = FieldCodec.encode(rows[0])
        with NativeEntry.open_evidence(selected) as evidence:
            _, entries = evidence.observe()
            users = [entry for entry in entries if entry.input_id == result.input_id]
            assert len(users) == 1
            receipt['actual_user_entry_id'] = users[0].id
        final = service.views.full_history()
        replies = [message for message in final if message.sender == owner.name and not message.notice]
        assert replies[-1].body.strip() == TOKEN
        assert selected.read_bytes().startswith(baseline)
        now = captured.require_current()
        assert (now.model, now.thinking_level, now.worktree, now.session_file,
                now.process_identity) == original_settings
        after = {str(file): digest(file) for file in (DONOR, proof)}
        assert before == after
        observations = tuple((wire / 'diagnostics').glob('*.requests.jsonl'))
        assert observations
        native_processes = {}
        for file in observations:
            for line in file.read_text().splitlines():
                packet = json.loads(line)
                if process := packet.get('native_process'):
                    identity = FieldCodec.decode(ProcessIdentity, process)
                    native_processes[(identity.pid, identity.start_time)] = identity
        assert native_processes
        assert all(not identity.alive() for identity in native_processes.values())
        receipt.update(state='SCOPED_CONFIGURED_SELECTED_NATIVE_PASS',
            source_and_proof_unchanged=after, original_settings_unchanged=True,
            remaining_resource_claims=0, turn_idle=True,
            native_children_retired=FieldCodec.encode(tuple(native_processes.values())),
            request_diagnostics=tuple(str(file) for file in observations),
            original_prefix_preserved=True)
    except BaseException as error:
        receipt.update(state='FAILED_NO_REPLAY', error=repr(error))
        (ROOT / 'error.txt').write_text(traceback.format_exc())
        raise
    finally:
        receipt['elapsed_seconds'] = time.monotonic() - started
        save()
        print(json.dumps({key: receipt[key] for key in ('state', 'elapsed_seconds', 'root')}), flush=True)


if __name__ == '__main__':
    asyncio.run(run())
