"""Real Pi RPC publishes committed channel progress before the final turn."""

from bus_history import read_history
from agent_comms.messages import NoticeRole
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
from agent_comms.comms import wire
from agent_comms.messages import Message, InfoKind
from agent_comms.threads import Thread


