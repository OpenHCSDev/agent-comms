"""Store locks derive their ordering from the declared owners, not a bus gate."""

import pytest

from agent_comms.store_files import SharedLock, StoreLock


def test_undeclared_store_rank_is_rejected_before_acquisition():
    with pytest.raises(AttributeError):
        with StoreLock.ordered({object(): SharedLock()}):
            pytest.fail("Unranked owner was authorized")
