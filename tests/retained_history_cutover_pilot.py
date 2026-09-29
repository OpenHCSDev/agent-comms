"""Actual archived files and real directory activation; only disk-full is injected."""
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from agent_comms.field_codec import FieldCodec
from agent_comms.historical_views import HistorySource
from agent_comms.private_registry_guard import PrivateRegistryGuard
from agent_comms.store_files import file_revision
from agent_comms.wire_log import WireLog

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
import retained_history_cutover as cutover


def actual_small_archives(root):
    """Copies the real proof-bearing archive twice; never writes the live source."""
    live=Path(os.environ['ACTUAL_COMMS_SOURCE_ROOT'])
    sources=tuple(FieldCodec.decode(HistorySource,value)
                  for value in json.loads((live/'history_sources.json').read_text()))
    original=min(sources,key=lambda source:(Path(source.root)/'bus.jsonl').stat().st_size)
    bindings=[]
    for index in range(2):
        destination=root/'history'/str(index)
        shutil.copytree(original.root,destination)
        (destination/'.registry-owner-guard').unlink()
        guard=PrivateRegistryGuard(destination/'registry.json',original.wire_root_id)
        guard.create_pending();guard.commit_initial()
        bindings.append(replace(original,root=str(destination),
            snapshot_bus_revision=file_revision(destination/'bus.jsonl'),
            snapshot_registry_revision=file_revision(destination/'registry.json')))
    manifest=root/'history_sources.json'
    manifest.write_text(json.dumps([FieldCodec.encode(source) for source in bindings]))
    return tuple(bindings)


def test_real_activation_rolls_back_on_manifest_disk_full(tmp_path,monkeypatch):
    root=tmp_path/'root'
    sources=actual_small_archives(root)
    before=(root/'history_sources.json').read_bytes()
    original_bytes=tuple((Path(source.root)/'bus.jsonl').read_bytes() for source in sources)
    output=tmp_path/'prepared'
    cutover.prepare(root,output)
    writes=cutover._atomic_write_text
    failures=iter((True,False))

    def disk_full_once(path,text,**kwargs):
        if next(failures):
            raise OSError(errno.ENOSPC,'Injected manifest disk full after actual directory swaps')
        return writes(path,text,**kwargs)

    monkeypatch.setattr(cutover,'_atomic_write_text',disk_full_once)
    with pytest.raises(OSError,match='disk full'):
        cutover.apply(root,output)
    assert (root/'history_sources.json').read_bytes()==before
    for source,raw in zip(sources,original_bytes,strict=True):
        source.validate()
        original=Path(source.root)
        assert (original/'bus.jsonl').read_bytes()==raw
        assert not original.with_name(original.name+'-retained-original').exists()
        assert original.with_name(original.name+'-current-prepared').is_dir()


def test_preparation_rejects_active_admission_range_without_changes(tmp_path):
    root=tmp_path/'root'
    sources=actual_small_archives(root)
    log=WireLog(Path(sources[0].root)/'bus.jsonl')
    marker=log.read_metadata_unlocked(required=True)
    marker.admission_after_seq=0
    log.write_metadata_unlocked(marker)
    before=(root/'history_sources.json').read_bytes()
    with pytest.raises(ValueError,match='no active admission range'):
        cutover.prepare(root,tmp_path/'prepared')
    assert (root/'history_sources.json').read_bytes()==before
