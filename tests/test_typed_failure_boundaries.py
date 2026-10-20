"""Failure ownership across the optional worker and observation task boundary."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from acp import RequestError

from agent_comms.context_segments.optional_awareness import OmittedAwareness
from agent_comms.optional_awareness_projection import OptionalAwarenessProjection
from agent_comms.ui_model.observation import OwnerRead, ReadOwnerGoal, ReadRefused


