"""Record the native selection result for the two retained cold-import scripts."""

import ast
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from nominal_refactor_advisor.codemod_module_declarations import SourceTopLevelSymbolMoveSelection

ROOT = Path('/home/ts/wt/comms-friction--P23--toad')
HEAD = '6587aee50bc73af398d16a33c6807c33bdeec038'
ARTIFACTS = Path(__file__).resolve().parent


@dataclass(frozen=True)
class ProbePlacement:
    name: str
    symbols: tuple[str, ...]

    @property
    def source(self) -> str:
        return f'tests/native/{self.name}.py'

    @property
    def destination(self) -> str:
        return f'tests/support/{self.name}.py'

    def record(self) -> dict[str, object]:
        source = subprocess.run(
            ['git', '-C', str(ROOT), 'show', f'{HEAD}:{self.source}'],
            check=True, capture_output=True, text=True,
        ).stdout
        result: dict[str, object] = {
            'source': self.source,
            'destination': self.destination,
            'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
            'symbols': self.symbols,
        }
        try:
            selection = SourceTopLevelSymbolMoveSelection.exact(
                str(ROOT / self.source), ast.parse(source), self.symbols,
            )
        except ValueError as error:
            result['status'] = 'refused'
            result['error'] = str(error)
        else:
            result['status'] = 'selected'
            result['whole_module'] = selection.takes_whole_module
        return result


records = [probe.record() for probe in (
    ProbePlacement('command_target_probe', ('declaration_name',)),
    ProbePlacement('goal_read_probe', ()),
)]
report = {
    'toad_commit': HEAD,
    'scope': 'Native declaration selection only; no simulation, tests, probe execution or product writes.',
    'records': records,
}
output = ARTIFACTS / 'probe-placement-native-selection.json'
output.write_text(json.dumps(report, indent=2) + '\n')
print(output)
print(json.dumps(report, indent=2))
