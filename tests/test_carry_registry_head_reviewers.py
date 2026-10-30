"""The one-shot registry carry preserves the document and grants no reviewer authority."""

import json

import pytest

from cutover.carry_registry_head_reviewers import RegistryHeadReviewers

from agent_comms.field_codec import FieldCodec
from agent_comms.locked_store import LiveFilesRequest
from agent_comms.registry_document import RegistryDocument
from agent_comms.registry_store import RegistryStore


def test_registry_format_carry_is_dry_run_safe_exact_and_idempotent(tmp_path):
    root = tmp_path / 'root'
    root.mkdir(mode=0o700)
    path = RegistryStore.path_in(root)
    current = FieldCodec.encode_record(RegistryDocument())
    version = FieldCodec.record_key(RegistryDocument, 'version')
    before = current | {version: 1}
    path.write_text(json.dumps(before))
    saved = path.read_bytes()
    step = RegistryHeadReviewers(LiveFilesRequest(root), tmp_path / 'spill')

    assert step.carry(apply=False).changed == 1
    assert path.read_bytes() == saved
    assert step.carry(apply=True).rewritten == (path.relative_to(root),)
    assert json.loads(path.read_text()) == current
    assert FieldCodec.decode_document(RegistryDocument, current) == RegistryDocument()
    carried = path.read_bytes()
    assert step.carry(apply=True).changed == 0
    assert path.read_bytes() == carried


@pytest.mark.parametrize('version', (0, 3))
def test_registry_carry_refuses_other_versions_without_rewriting(tmp_path, version):
    root = tmp_path / 'root'
    root.mkdir(mode=0o700)
    path = RegistryStore.path_in(root)
    document = FieldCodec.encode_record(RegistryDocument())
    document[FieldCodec.record_key(RegistryDocument, 'version')] = version
    path.write_text(json.dumps(document))
    saved = path.read_bytes()
    step = RegistryHeadReviewers(LiveFilesRequest(root), tmp_path / 'spill')
    with pytest.raises(ValueError):
        step.carry(apply=True)
    assert path.read_bytes() == saved
