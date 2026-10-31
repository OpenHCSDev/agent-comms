"""The installed NominalRefactorAdvisor counting plan evidence.

Only readers of plans import this module; the plan declarations and the bus never load the
advisor.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from nominal_refactor_advisor import audit
from nominal_refactor_advisor.ast_tools import ParsedModule
from nominal_refactor_advisor.audit.findings import GodFunction
from nominal_refactor_advisor.audit.source_measures import ForeignAbsenceProbe

from agent_comms.plan_reading import PlanReadings
from agent_comms.phase_evidence import Census, MainModule


@dataclass(frozen=True)
class NominalCensus(Census):
    def god_function(self, module: MainModule,
                     function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        parsed = ParsedModule(path=Path(module.file_path), module_name=module.file_path,
                              module=module.module, source=module.source)
        return any(GodFunction.from_node(function, parsed))

    def absence_probes(self, node: ast.AST) -> int:
        return ForeignAbsenceProbe.occurrences(node)

    def audit_package(self) -> Path:
        return Path(audit.__path__[0])


class NominalPlanReadings(PlanReadings):
    census: ClassVar[NominalCensus] = NominalCensus()
