"""OpenHCS agent communications — import each API from its defining module."""

__version__ = "0.1.0"

# Every public entrypoint, including standalone stores, validates the full family.
from . import store_order

# isort: split
# File declarations must exist before a standalone inventory, not only after a
# particular reader, native turn or background index has first been opened.
from . import (  # noqa: F401
    native_prompt_binding,
    selected_write_plan,
    view_unread,
    wake_candidate_index,
)
from .child_process import ChildLaunch
from .runtime_children import RuntimeChildrenStore

ChildLaunch.children_store = RuntimeChildrenStore
