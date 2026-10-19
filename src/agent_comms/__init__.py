"""OpenHCS agent communications — import each API from its defining module."""

__version__ = "0.1.0"

# Bind the process/store dependency once before validating the store family.
# Every public entrypoint, including standalone stores, validates the full family.
from . import comms, store_order
from .child_process import ChildLaunch
from .runtime_children import RuntimeChildrenStore

ChildLaunch.children_store = RuntimeChildrenStore
