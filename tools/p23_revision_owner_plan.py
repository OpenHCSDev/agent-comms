"""Author the retained revision-owner migration for NRA's transaction runner."""

import ast
import io
import json
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src/agent_comms"


def declaration(path: Path, name: str) -> ast.FunctionDef:
    nodes = ast.parse(path.read_text()).body
    for part in name.split("."):
        node, = (node for node in nodes if isinstance(node, (ast.ClassDef, ast.FunctionDef))
                 and node.name == part)
        nodes = node.body
    if not isinstance(node, ast.FunctionDef):
        raise TypeError(name)
    return node


def source(path: Path, name: str) -> str:
    node = declaration(path, name)
    start = min((node.lineno, *(item.lineno for item in node.decorator_list)))
    return "".join(path.read_text().splitlines(keepends=True)[start - 1:node.end_lineno])


def rebound_body(path: Path, name: str, bindings: dict[str, str]) -> str:
    node = declaration(path, name)
    for statement in node.body:
        for child in ast.walk(statement):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                raise ValueError(f"Nested scope requires separate binding proof: {name}")
            if isinstance(child, ast.Name) and child.id in bindings and isinstance(child.ctx, ast.Store):
                raise ValueError(f"Reassigned parameter requires separate proof: {name}:{child.id}")
    lines = path.read_text().splitlines(keepends=True)[node.body[0].lineno - 1:node.end_lineno]
    tokens = tuple(tokenize.generate_tokens(io.StringIO("".join(lines)).readline))
    for token in reversed(tokens):
        if token.type == tokenize.NAME and token.string in bindings:
            row, column = token.start
            end_row, end_column = token.end
            if row != end_row:
                raise ValueError("Unexpected multiline identifier")
            line = lines[row - 1]
            lines[row - 1] = line[:column] + bindings[token.string] + line[end_column:]
    return "".join(lines)


def patch(path: Path, pairs: list[tuple[str, str]]) -> dict[str, object]:
    return {"operation": "patch_target", "file_path": str(path),
            "replacements": [{"old_source": before, "new_source": after}
                             for before, after in pairs]}


def imported(path: Path, statement: str) -> dict[str, object]:
    return {"operation": "ensure_import", "file_path": str(path), "import_source": statement}


def stage(identity: str, reason: str, operations: list[dict[str, object]]) -> dict[str, object]:
    return {"recipes": [{"recipe_id": identity, "reason": reason, "operations": operations,
                         "architecture_guards": [], "authority_claims": []}],
            "architecture_guards": []}


def main() -> None:
    presentation = SRC / "presentation.py"
    history = SRC / "history_views.py"
    observation = SRC / "ui_model/observation.py"
    attribution = rebound_body(presentation, "WireRevision.changed_threads",
                               {"self": "revision", "previous": "self"})
    attribution = (
        "    def changed_threads(self, revision: WireRevision, comms: Comms,\n"
        "                        activity: Mapping[str, ObservedActivity | DeletedThread],\n"
        "                        snapshot: RegistrySnapshot, registry_changed: frozenset[str],\n"
        "                        ) -> frozenset[str]:\n" + attribution
    )
    capture = rebound_body(presentation, "WireRevision.capture",
                           {"cls": "WireRevision", "views": "self"})
    capture = capture.replace("        captured = {", "        expiry_tick = int(time.time())\n        captured = {", 1)
    capture = "    def revision(self) -> WireRevision:\n" + capture
    source_replacements = [
        (source(presentation, "WireRevision.capture"), ""),
        (source(presentation, "WireRevision.changed_threads"), ""),
        ("def revision_source(paths: Callable[[HistoryViews], tuple[Path, ...]],",
         "def revision_source(paths: Callable[..., tuple[Path, ...]],"),
        ("def source_paths(cls, views: HistoryViews)", "def source_paths[Source](cls, views: Source)"),
        ("    from agent_comms.comms import Comms\n", ""),
        ("    from agent_comms.history_views import HistoryViews\n", ""),
        ("    from agent_comms.ui_model.observation import ThreadsObserved\n", ""),
        ("return before // WireRevision.expiry_seconds != now // WireRevision.expiry_seconds",
         "return WireRevision.period_for(before) != WireRevision.period_for(now)"),
        ("    @property\n    def expiry_period(self) -> int:\n        return self.expiry_tick // self.expiry_seconds",
         "    @classmethod\n    def period_for(cls, tick: int) -> int:\n        return tick // cls.expiry_seconds\n\n"
         "    @property\n    def expiry_period(self) -> int:\n        return self.period_for(self.expiry_tick)"),
    ]
    for helper, expression in (
        ("room_ledger_paths", "RoomLedger.source_paths(views.registry.storage_rule.room_ledger.root)"),
        ("relationship_paths", "(relationships.RelationshipStore.path_in(views.root),)"),
        ("notification_paths", "NotificationAssignment.source_paths(views.root)"),
        ("external_transcript_paths", "ExternalTranscript.source_paths(views.root)"),
        ("team_frame_paths", "TeamFrameFile.source_paths(views.root)"),
    ):
        source_replacements.extend([
            (source(presentation, helper), ""),
            (f"revision_source({helper},", f"revision_source(lambda views: {expression},"),
        ])
    tests = '''"""Runtime contracts of the original live and retained revision owners."""

from dataclasses import replace
from typing import get_type_hints

import pytest

from agent_comms.history_views import HistoryViews
from agent_comms.presentation import WireRevision, expiry_period_changed, revision_source
from agent_comms.ui_model.observation import ThreadsObserved


def test_revision_owner_callbacks_resolve() -> None:
    assert get_type_hints(HistoryViews.revision)["return"] is WireRevision
    assert get_type_hints(ThreadsObserved.changed_threads)["revision"] is WireRevision
    paths = WireRevision.source_paths
    assert get_type_hints(paths)["views"] is paths.__type_params__[0]
    get_type_hints(revision_source)
    get_type_hints(WireRevision.period_for)


@pytest.mark.parametrize("before,now", [(0, 29), (29, 30), (30, 59), (59, 60), (-1, 0)])
def test_revision_period_uses_its_owned_projection(before: int, now: int) -> None:
    original = WireRevision.empty()
    before_period = replace(original, expiry_tick=before).expiry_period
    now_period = replace(original, expiry_tick=now).expiry_period
    assert WireRevision.period_for(before) == before_period
    assert WireRevision.period_for(now) == now_period
    assert expiry_period_changed(before, now) == (before_period != now_period)
'''
    acp = ROOT / "tests/test_acp.py"
    activity_test = source(acp, "TestActivityLayer.test_emit_and_read_current")
    old_capture = "WireRevision.capture(wired.views, expiry_tick=0)"
    if activity_test.count(old_capture) != 3:
        raise ValueError("Original activity test no longer has its three capture calls")
    migrated_activity_test = activity_test.replace(
        "        from agent_comms.presentation import WireRevision\n", "",
    ).replace(old_capture, "wired.views.revision()")
    result = {"stages": [
        stage("p23-revision-live-and-retained-owners",
              "Admission: HistoryViews.revision is the original live capture caller; ThreadsObserved owns the retained results, activity and revision consumed by attribution. Move the exact bodies, rebinding only their non-reassigned parameters. Preserve strict reads, held registry snapshots, route attribution, cancellation and process placement. Capture the expiry clock before statting sources, exactly as the old caller evaluated the argument. No wire fields move and no substitute records, stores or source contracts are introduced. Authored receiver rebinding still requires original-consumer proof.", [
                  imported(history, "from agent_comms.watched import FileRevision"),
                  imported(history, "from agent_comms.recorded_bus import RecordedBusIdentity"),
                  imported(observation, "from agent_comms.messaging.channel_targets import CommandTarget"),
                  imported(observation, "from agent_comms.revision_views import BusSourceView"),
                  patch(history, [(source(history, "HistoryViews.revision"), capture)]),
                  patch(observation, [("    def revision_inputs(self) -> tuple[dict[str, AcquiredThreadPresentation], set[str]]:",
                                       attribution + "\n    def revision_inputs(self) -> tuple[dict[str, AcquiredThreadPresentation], set[str]]:")]),
              ]),
        stage("p23-revision-value-keeps-value-and-metadata-operations",
              "Delete moved entry points and their HistoryViews/Comms/ThreadsObserved imports. WatchPaths already declares its callback as Callable[..., tuple[Path, ...]]; revision_source stores that existing directive contract. source_paths is generic metadata application, not a live-view operation. Inline five sole-use projections into existing field metadata, retaining the same determining store methods and arguments. Centralize identical expiry arithmetic, preserving original class receivers. The remaining MessageBus/Goals guarded contracts are explicitly unfinished dependency work.", [patch(presentation, source_replacements)]),
        stage("p23-original-revision-callers-and-runtime-proof",
              "Production and regression attribution calls have side-effect-free local revision/previous receivers; migrate all exact calls with unchanged arguments and assertions. The ACP regression protects activity changes only, so use HistoryViews.revision rather than manually choosing zero expiry. New runtime tests import only original classes, letting the source-hidden control collect and expose missing callbacks rather than fail imports.", [
                  patch(observation, [("changed = revision.changed_threads(previous, comms, activity, snapshot, registry_changed)",
                                       "changed = previous.changed_threads(revision, comms, activity, snapshot, registry_changed)")]),
                  patch(ROOT / "tests/test_thread_read_results.py", [("assert later.changed_threads(\n        previous, comms, activity, snapshot, earlier_registry.changed_names_to(snapshot),",
                                                                      "assert previous.changed_threads(\n        later, comms, activity, snapshot, earlier_registry.changed_names_to(snapshot),")]),
                  patch(acp, [(activity_test, migrated_activity_test)]),
                  {"operation": "create_file", "file_path": str(ROOT / "tests/test_revision_owner_annotations.py"), "source": tests},
              ]),
    ]}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
