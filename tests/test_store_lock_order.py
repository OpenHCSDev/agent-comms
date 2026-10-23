"""Store locks derive their ordering from the declared owners, not a bus gate."""

import pytest

from agent_comms import store_order
from agent_comms.store_files import SharedLock, StoreLock, StoreLockOwner


def test_undeclared_store_rank_is_rejected_before_acquisition():
    with pytest.raises(AttributeError):
        with StoreLock.ordered({object(): SharedLock()}):
            pytest.fail("Unranked owner was authorized")


def test_complete_tuple_derives_every_rank():
    assert set(store_order.STORE_LOCK_ORDER) == set(StoreLockOwner.members_with(StoreLockOwner))
    for rank, owner in enumerate(store_order.STORE_LOCK_ORDER):
        assert vars(owner)["lock_rank"] == rank
