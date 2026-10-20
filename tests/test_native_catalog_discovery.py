"""Catalogs through actual pinned Pi, without a provider prompt or saved input."""


import pytest

import agent_comms.backend as backend


@pytest.mark.refactor_guard
def test_backend_no_longer_owns_discovery_or_catalog_projection():
    assert not {"discover_models", "discover_thinking_levels", "Model"}.intersection(vars(backend))


