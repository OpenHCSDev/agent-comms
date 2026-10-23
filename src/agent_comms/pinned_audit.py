"""The pinned refactor-audit package, loaded once from the skill this package ships.

The census measures and finding thresholds that the debt ratchet and the plan phases both
judge by come from here, so neither restates a number the audit owns."""

from __future__ import annotations

import sys
from importlib.resources import files
from pathlib import Path

from agent_comms import skills

SCRIPTS = Path(str(files(skills) / "refactor-audit" / "scripts"))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit as audit  # noqa: E402
from audit.findings import GodFunction as GodFunction  # noqa: E402
from audit.findings import ParsedModule as ParsedModule  # noqa: E402
from audit.measures import ForeignAbsenceProbe as ForeignAbsenceProbe  # noqa: E402
