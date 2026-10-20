"""Physical native parent -> normal fork -> first ACP send, without replay."""
from agent_comms.pi_vocabulary import OffThinkingLevel

import asyncio
import json
import os
import time
from abc import abstractmethod
from pathlib import Path

import pytest
from acp.schema import TextContentBlock

from agent_comms.relationships import RelationshipEdit
from agent_comms.acp import CommsClient
from agent_comms.acp_extension import (
    CompactionChangedUpdate,
    CompactionCommittedUpdate,
    InputFailedUpdate,
    RequestFailedUpdate,
    decode_updates,
)
from agent_comms.comms import Comms
from agent_comms.declared_family import DeclaredFamily
from agent_comms.input_disposition import InputDispositions
from agent_comms.pi_commands import GetSessionStats, GetState
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.thread_management import ForkSpec
from agent_comms.threads import Thread
from test_coordinated_runtime import tmp_path as private_root_fixture
from stored_inputs import stored_inputs

tmp_path = private_root_fixture


class OwnerClaimCase(DeclaredFamily, affix="Case"):
    @abstractmethod
    async def create(self, comms): ...

    @abstractmethod
    async def first_input(self, attachment, child): ...

    @abstractmethod
    def verify_fraction(self, fraction): ...

    @abstractmethod
    def verify_history(self, native, thread, original): ...

    def verify_relationships(self, comms, thread):
        assert comms.registry.require(thread.name).incarnation == thread.incarnation


class OrdinaryOwnerCase(OwnerClaimCase):
    repetitions = 2400

    async def create(self, comms):
        await asyncio.to_thread(comms.owners.start, "physical-parent")
        return comms.registry.require("physical-parent")

    async def first_input(self, attachment, child):
        return await attachment.prompt(
            child.name, [TextContentBlock(type="text", text="hey Boss")]
        )

    def verify_fraction(self, fraction):
        assert 0.60 < fraction < 0.70

    def verify_history(self, native, thread, original):
        assert Path(thread.session_file) == native.session
        assert native.session.read_bytes().startswith(original)


class ForkOwnerCase(OwnerClaimCase):
    repetitions = 900

    async def create(self, comms):
        parent = comms.registry.require("physical-parent")
        return await asyncio.to_thread(
            comms.threads.create,
            ForkSpec.from_task(
                name="physical-child",
                project=parent.worktree,
                model=parent.model,
                thinking_level=parent.thinking_level,
                parent="physical-parent",
                task="hey Boss",
            ),
        )

    async def first_input(self, attachment, child):
        # Normal fork already journaled and dispatched the first input.
        # A second ACP prompt would test queueing instead of fork first-send.
        return None

    def verify_fraction(self, fraction):
        assert 0.20 < fraction < 0.27

    def verify_history(self, native, thread, original):
        assert Path(thread.session_file) != native.session
        assert native.session.read_bytes() == original

    def verify_relationships(self, comms, thread):
        super().verify_relationships(comms, thread)
        parent = comms.registry.require(thread.parent)
        assert parent.name == "physical-parent"
        edge = comms.relationships.edit(parent.name, RelationshipEdit.member_named('add'), thread.name, "Actual native fork")
        assert edge.pair_identity == frozenset((parent.incarnation, thread.incarnation))
        for owner, peer in ((parent, thread), (thread, parent)):
            saved = comms.relationships.collaborations(owner.name)
            assert len(saved) == 1
            assert saved[0].counterpart(owner) == peer.incarnation
        reopened = Comms(comms.root)
        assert reopened.relationships.collaborations(parent.name) == (edge,)
        assert reopened.registry.require(parent.name) == parent


