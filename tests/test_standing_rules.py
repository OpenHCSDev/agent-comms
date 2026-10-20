import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agent_comms.change_report import FileLine
from agent_comms.child_process import ProcessIdentity
from agent_comms.commander.config import CommanderConfig
from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.plan_phases import RecordPlanePhase, ReviewQueuePhase, UnplannedPhase
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry
from agent_comms.standing_rules import (BoardDisagrees, BridgeDown, DiskLow, HuntDue, MemoryLow, PlanIdle,
    RulesDue, StandingRule, OrderOverdue, FirstCallMissing, OwnerIdle)
from agent_comms.storage_rules import MemoryHolder, UnitStorage
from agent_comms.thread_execution import ExternalThreadExecution, NativeThreadExecution
from agent_comms.thread_status import StoppedThreadStatus
from agent_comms.todos import (CleanHunt, DirectiveText, MergedProgress,
    MergedPullRequest, NoCommitProgress, OpenPrProgress, OrderProgress, PullRequests, StrikeReason,
    Todo, UnmergedPullRequest, UnreadProgress)
from agent_comms.connector import CALLER
from agent_comms.tools import CommsOrderServeTool, CommsRuleReviewTool, CommsSendTool, CommsStandingRulesTool, CommsChangeProblemTool

from test_commander_problem_orders import create, native_judge_edits


def lines(comms, member, commander="lead"):
    return [found.line() for found in member.violations(comms, commander)]


def working(comms, name, created):
    """A unit whose native session records a turn now."""
    from datetime import UTC, datetime
    stamp = datetime.now(UTC).isoformat()
    path = comms.root / f"{name}-session.jsonl"
    rows = [dict(type="session", id=f"{name}-session", version=3, timestamp=stamp),
            dict(type="message", id=f"{name}-input", timestamp=stamp,
                 message=dict(role="user", content="Repair the owner",
                              timestamp=time.time() * 1000))]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    if name in comms.registry.all_threads():
        comms.registry.register(replace(comms.registry.require(name), session_file=str(path)))
    else:
        comms.registry.declare(comms.registry.new_thread(
            name, frozenset(), str(comms.root), session_file=str(path), created_at=created))


def test_plain_messages_need_no_order_labels():
    assert DirectiveText.refused("Yes, go ahead.") == ()


