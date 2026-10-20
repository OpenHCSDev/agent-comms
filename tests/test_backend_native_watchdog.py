"""Actual pinned Pi outcomes; no injected RPC events or paid provider requests."""

import asyncio
import json

import pytest
from agent_comms.turn_watchdog import ModelWait

import agent_comms.agent_events as events
import agent_comms.backend as backend
from agent_comms.agent_backend import InputContent, InputId, InputRequest, SendNow, TurnInbox, WhenBusy


