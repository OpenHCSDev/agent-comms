"""Real native Pi goal tool, localhost provider, and dependency-triggered owner turn."""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_comms.acp import CommsAgent
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.goal_actions import SetGoalAction
from agent_comms.goal_generation import CompletedGeneration
from agent_comms.goal_presentation import GoalExecutionState
from agent_comms.threads import Thread
from stored_inputs import stored_inputs


