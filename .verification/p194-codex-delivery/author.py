import ast,json
from pathlib import Path
r=Path('/home/ts/wt/codex-live--P181/src/agent_comms');ops=[]
def patch(f,t,o,n):ops.append(dict(operation='patch_target',file_path=str(r/f),target_qualname=t,replacements=[dict(old_source=o,new_source=n)]))
def imp(f,s):ops.append(dict(operation='ensure_import',file_path=str(r/f),import_source=s))
def insert(f,t,s):ops.append(dict(operation='insert_after_target',file_path=str(r/f),target_qualname=t,source=s))
f='cli_bridges/harness_delivery_states.py'
patch(f,'AcceptedOutcome','class AcceptedOutcome(DeliveredOutcome):','class AcceptedOutcome(Outcome):')
imp(f,'from agent_comms.execution.turn_item import NativeItemId, NativeTurnId')
s=(r/f).read_text();n=next(n for n in ast.parse(s).body if isinstance(n,ast.ClassDef) and n.name=='ObservedMonitor');old=ast.get_source_segment(s,n)
methods=old[old.index('    def evidence'):]
new='''class ObservedInputEvidence(MonitorEvidence, Timestamped, declared_name=None):
    """A complete original input observed in its harness, with its source retained."""

'''+methods+'''

@dataclass(frozen=True)
class ObservedMonitor(ObservedInputEvidence):
    record_id: TranscriptRecordId
    timestamp: float


@dataclass(frozen=True)
class ObservedBridgeInput(ObservedInputEvidence):
    turn_id: NativeTurnId
    item_id: NativeItemId
    timestamp: float'''
# The existing decorator belongs on the two stored record leaves, not the behavior parent.
patch(f,'agent_comms.cli_bridges.harness_delivery_states','@dataclass(frozen=True)\n'+old,new)
f='cli_bridges/shared.py'
imp(f,'from agent_comms.cli_bridges.harness_delivery_states import ObservedBridgeInput')
patch(f,'BridgeInputObserved','    text: str\n','    text: str\n    record: ObservedBridgeInput\n')
patch(f,'BridgeInputObserved.observed','partial(replace, outcome=DeliveredOutcome)','partial(replace, outcome=DeliveredOutcome, monitor=self.record)')
f='cli_bridges/codex_protocol.py'
imp(f,'from agent_comms.cli_bridges.harness_delivery_states import ObservedBridgeInput')
imp(f,'from agent_comms.cli_bridges.runtime_updates import FailedTurn')
imp(f,'from agent_comms.execution.provider_records import CodelessProviderDiagnosticError')
imp(f,'import time')
patch(f,'BusClientId.completed','''        return BridgeInputObserved(events, self.message_id, item.input_text,
                                   source_turn=NamedSourceTurn(turn_id))''','''        return BridgeInputObserved(
            events, self.message_id, item.input_text,
            ObservedBridgeInput(turn_id, item.id, events[0].timestamp),
            source_turn=NamedSourceTurn(turn_id),
        )''')
# Error is nullable on Codex's wire. Decode that external distinction here only.
insert(f,'TurnHeader','''class TurnError(WireModel):
    message: str


class CompletedTurnHeader(TurnHeader):
    error: TurnError | None = None

    def finished(self) -> RuntimeUpdate:
        if self.error is None:
            return FinishedTurn(self.turn_id)
        return FailedTurn(self.turn_id, CodelessProviderDiagnosticError(self.error.message), time.time())''')
patch(f,'TurnFinished','    turn: TurnHeader','    turn: CompletedTurnHeader')
patch(f,'TurnFinished.apply','return FinishedTurn(self.turn.turn_id)','return self.turn.finished()')
f='cli_bridges/runtime_updates.py'
imp(f,'from agent_comms.execution.provider_records import ProviderDiagnosticError')
imp(f,'from agent_comms.execution.turn_phase import FailedPhase')
imp(f,'from agent_comms.transcript.transcript_events import NoticeTranscript')
insert(f,'FinishedTurn','''@dataclass(frozen=True)
class FailedTurn(FinishedTurn):
    """Retire the original turn and keep its actual error visible without a reply or retry."""

    error: ProviderDiagnosticError
    timestamp: float

    def publish(self, output: ThreadOutput) -> None:
        super().publish(output)
        item_id = NativeItemId(f"turn-error:{self.turn_id}")
        output.publish_item(FinishedItem(self.turn_id, item_id, (
            NoticeTranscript(self.error.message, position=item_id, timestamp=self.timestamp),
        )))
        output.enter(FailedPhase(detail=self.error.message, error=self.error))''')
Path('/home/ts/.cache/agent-scratch/codex-latency/p193-bridge-reporting/fix.plan.json').write_text(json.dumps({'recipes':[dict(recipe_id='preserve-harness-observations-and-turn-errors',operations=ops)]},indent=2))
