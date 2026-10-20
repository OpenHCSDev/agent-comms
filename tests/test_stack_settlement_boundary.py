"""Real Pi: old settlement cannot terminate an identified late follow-up."""

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import agent_comms.agent_events as ae
import agent_comms.backend as backend
from agent_comms.agent_backend import InputContent, InputId, InputRequest, WhenBusy
from agent_comms.pi_native_backend import PersistentPiSession


