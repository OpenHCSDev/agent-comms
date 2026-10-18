"""The layer map in docs/layout is the layer script's output, never a hand edit."""
from module_layers import ROOT, Package


def test_module_layers_doc_matches_its_output():
    assert (ROOT / 'docs/layout/MODULE_LAYERS.md').read_text() == Package(ROOT).markdown()
