"""Store locks derive their ordering from the declared owners, not a bus gate."""

import pytest

from agent_comms.store_files import SharedLock, StoreLock, StoreLockOwner
from agent_comms.store_order import STORE_LOCK_ORDER


def test_undeclared_store_rank_is_rejected_before_acquisition():
    with pytest.raises(KeyError):
        with StoreLock.ordered({object(): SharedLock()}):
            pytest.fail("Unranked owner was authorized")


def test_complete_tuple_derives_every_rank():
    ranks = StoreLockOwner.lock_ranks()
    assert set(ranks) == set(StoreLockOwner.members_with(StoreLockOwner))
    assert ranks == {owner: rank for rank, owner in enumerate(STORE_LOCK_ORDER)}
