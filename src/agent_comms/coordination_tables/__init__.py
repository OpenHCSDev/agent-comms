"""Load installed coordinator row declarations for TypedTable family discovery.

This package exposes no aggregate imports. Callers import the owning row module.
"""

from importlib import import_module
from pkgutil import iter_modules

for _module in iter_modules(__path__, __name__ + "."):
    import_module(_module.name)
