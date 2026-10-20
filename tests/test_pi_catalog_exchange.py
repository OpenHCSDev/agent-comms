"""Recorded peer tests of request correlation; native acceptance is separate."""

import asyncio
import json
from dataclasses import dataclass

import pytest

from agent_comms.pi_commands import CatalogQuery, PiCommand
from agent_comms.pi_payloads import ModelsData
from agent_comms.pi_rpc import PiRpcChannel


