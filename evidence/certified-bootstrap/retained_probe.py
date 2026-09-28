"""Certify owned copies of actual retained current roots; never activate them."""
import json
import shutil
from dataclasses import replace
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.coordinated_runtime_schema import install_native_runtime_schema
from agent_comms.cohort_schema import install_private_cohort_schema
from agent_comms.coordination_store import MutationStore
from agent_comms.native_source_cursor import _bounded_coverage_pages, _source_witness
from agent_comms.private_bus_checkpoint import PrefixWitness, install_private_bus_checkpoint
from agent_comms.wire_log import WireLog

source = Path('/home/ts/wt/comms-acp-saved-session-startup-20260928/.artifacts/d22-integrated-current')
stage = Path('.artifacts/retained-current').resolve()
for number, original in enumerate((source, source / 'history/source-0', source / 'history/source-1')):
    root = stage / str(number)
    root.mkdir(mode=0o700, parents=True)
    before = (original / 'bus.jsonl').read_bytes()
    for name in ('bus.jsonl', 'bus_meta.json', 'registry.json', '.registry-owner-guard'):
        shutil.copy2(original / name, root / name)
    log = WireLog(root / 'bus.jsonl')
    marker = log.read_metadata_unlocked(required=True)
    log.write_metadata_unlocked(replace(marker, checkpoint_version=None, checkpoint_seal=None))
    install_private_bus_checkpoint(log)
    comms = Comms(root)
    witness = _source_witness(comms.bus)
    assert isinstance(witness, PrefixWitness)
    assert witness.root_id == marker.root_id and witness.through_seq == marker.last_seq
    assert (root / 'bus.jsonl').read_bytes() == before
    rows = comms.bus.log.full_history()
    with MutationStore(str(root / 'coordination.sqlite3')) as store:
        install_private_cohort_schema(store)
        install_native_runtime_schema(store)
        coverage = _bounded_coverage_pages(comms.bus, store, marker.root_id, '0' * 32)
        assert coverage.covered_seq == 0 and coverage.injected_source_seqs == ()
    assert (original / 'bus.jsonl').read_bytes() == before
    print(json.dumps({'source': str(original), 'rows': len(rows), 'bytes': len(before),
                      'initial_highwater': witness.latest_initial_seq,
                      'admission_after_seq': marker.admission_after_seq,
                      'native_coverage': coverage.covered_seq, 'source_unchanged': True}), flush=True)
print('PASS: retained root source certificates and empty post-floor proof', flush=True)
