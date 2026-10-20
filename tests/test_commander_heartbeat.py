"""Heartbeat acceptance uses the actual rule family, worker, publication and inbox."""

import asyncio
import json
import os
from contextlib import contextmanager, nullcontext
from dataclasses import replace

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.coordinator import Coordination
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goals import RequestGoalOrder
from agent_comms.rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry, RuleReviews
from agent_comms.standing_rules import DiskFloorLow, DiskLow, RulesDue
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_execution import ExternalThreadExecution
from agent_comms.thread_status import StoppedThreadStatus
from test_commander_problem_orders import create
from test_order_progress import checkout, commit, git


def external_commander(comms, name):
    comms.registry.declare(replace(comms.registry.require(name), execution=ExternalThreadExecution))
    return comms.registry.external_process(name, ProcessIdentity.capture(os.getpid()))


