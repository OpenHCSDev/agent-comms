import asyncio
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent_comms.comms import Comms
from agent_comms.channel_targets import CommandTarget
from agent_comms.goals import CountCheckResult
from agent_comms.active_route import active_route_path
from agent_comms.commander import standing
from agent_comms.commander.config import CommanderConfig
from agent_comms.connector import Connector
from agent_comms.connector_store import ConnectorStore
from agent_comms import coordinator
from agent_comms.thread_identity import StallWatcherIncarnation
from agent_comms.todos import (BlockedTodoState, ChangeTodoState, CorrectCheckTodo,
                              MissedDeadlineStrike, MissedFirstCallStrike,
                              ReleaseTodo, StallStrike, StrikeReason, TodoConflict)
from agent_comms.storage_rules import StorageRule
from agent_comms.tool_caller import ThreadCaller
from agent_comms.tools import CommsStandingRulesTool
from test_commander_problem_orders import create
from test_order_progress import checkout, commit, git
from test_standing_rules import working


