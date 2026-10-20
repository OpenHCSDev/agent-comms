"""The installer names all scratch-route readers and waits for both to leave."""
import asyncio
import fcntl
import os
from contextlib import AsyncExitStack
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading

import agent_comms.active_route as active_route
import agent_comms.private_nk_entrypoint as private_nk_entrypoint
from agent_comms.child_process import AttachedChild
from agent_comms.comms import Comms


