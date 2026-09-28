"""Read actual catalog sources only; migrate owned metadata copies, retain no chat content."""
import json
import tempfile
from pathlib import Path

import importlib.util
import sys

from agent_comms.catalog_store import ChannelCatalog

spec = importlib.util.spec_from_file_location("channel_cutover", Path(__file__).resolve().parents[2] / "tools/cutover/channel_catalog.py")
cutover_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cutover_module
spec.loader.exec_module(cutover_module)
CatalogMigration = cutover_module.CatalogMigration
from agent_comms.field_codec import FieldCodec

SOURCES = (
    Path('/home/ts/.agent-comms'),
    Path('/var/tmp/agent-comms-live-20260927-6_d_vdul'),
    Path('/var/tmp/agent-comms-live-20260927-wzjtqhza'),
)


def main():
    report = []
    for source in SOURCES:
        paths = (source / ChannelCatalog.filename, *CatalogMigration.paths(source))
        originals = {p.name: p.read_bytes() for p in paths if p.exists()}
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '.artifacts') as temporary:
            root = Path(temporary)
            for name, data in originals.items():
                (root / name).write_bytes(data)
            catalog = ChannelCatalog(root / ChannelCatalog.filename)
            document = cutover_module.read_source(root)
            if 'catalog.json' in originals:
                old = json.loads(originals['catalog.json'])
                assert FieldCodec.encode(document.preferences) == old['preferences']
                assert document.tags == frozenset(old['tags'])
                assert document.list_order.value == old['list_order']
                assert all(document.resolve(name).tags == frozenset(tags)
                           for name, tags in old['audiences'].items())
            else:
                old = json.loads(originals.get('channels.json', b'{}'))
                for name, order in old.get('orders', {}).items():
                    assert document.resolve(name).order.value == order
                for name, created in old.get('created_at', {}).items():
                    assert document.resolve(name).created_at == created
            cutover_module.cutover(root)
            assert 'audiences' not in json.loads(catalog.path.read_text())
            assert catalog.read() == document
            assert FieldCodec.decode(type(document), json.loads(catalog.path.read_text())) == document
            report.append({'source': str(source), 'metadata_files': len(originals),
                           'preferences': len(document.preferences),
                           'saved_views': len(document.saved_views),
                           'lossless_projection_and_reopen': True})
        assert originals == {p.name: p.read_bytes() for p in paths if p.exists()}
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2] / '.artifacts') as temporary:
        root = Path(temporary)
        (root / 'catalog.json').write_text(json.dumps({
            'tags': ['api', 'ui'], 'audiences': {'#engineering': ['api', 'ui']},
            'preferences': {'#engineering': {'pinned': True, 'created_at': 17,
                'order': 'last_activity', 'parent': '#ui', 'pinned_threads': ['agent']}},
            'saved_views': {}, 'list_order': 'name'}))
        retained_wire = b'original durable history bytes\n'
        (root / 'bus.jsonl').write_bytes(retained_wire)
        result = cutover_module.cutover(root)
        assert result.saved_views['engineering'].original_targets == {'#engineering'}
        assert result.resolve('#engineering').pinned
        assert result.resolve('#engineering').created_at == 17
        assert result.pinned_threads('#engineering') == {'agent'}
        assert ChannelCatalog(root/'catalog.json').read() == result
        assert (root/'bus.jsonl').read_bytes() == retained_wire
    print(json.dumps({'sources_unchanged': True, 'roots': report,
        'synthetic_union_preferences_original_target_and_wire_preserved': True}, indent=2))


if __name__ == '__main__':
    main()
