"""Relevance between threads is declared and decided by its owners and nowhere else."""

import ast
from pathlib import Path

import pytest

import agent_comms
import agent_comms.peer_relevance as peer_relevance
import agent_comms.relationships as relationships
from agent_comms.peer_relevance import PeerRelevance, RelevanceCapability
from agent_comms.relationships import MutualThreadRelation

pytestmark = pytest.mark.refactor_guard

PACKAGE = Path(agent_comms.__file__).parent
OWNERS = {Path(relationships.__file__), Path(peer_relevance.__file__)}


def _subclasses(root):
    found, pending = set(), [root]
    while pending:
        current = pending.pop()
        found.add(current.__name__)
        pending.extend(current.__subclasses__())
    return found


RELATIONS = _subclasses(MutualThreadRelation)
RELEVANCE = _subclasses(PeerRelevance) | _subclasses(RelevanceCapability)


def _consumer_trees():
    for path in sorted(PACKAGE.rglob("*.py")):
        if path not in OWNERS:
            yield path, ast.parse(path.read_text())


def _names(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }


def test_no_consumer_inspects_relation_or_relevance_types():
    """Consumers ask ``grants``; they never test what kind of relation or member they hold."""
    for path, tree in _consumer_trees():
        for node in ast.walk(tree):
            tested = ()
            if isinstance(node, ast.Call) and ast.unparse(node.func) in {"isinstance", "issubclass"}:
                tested = node.args[1:]
            elif isinstance(node, ast.Compare) and any(
                isinstance(op, (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)) for op in node.ops
            ):
                tested = (node.left, *node.comparators)
            elif isinstance(node, ast.match_case):
                tested = (node.pattern,)
            for expression in tested:
                assert not _names(expression) & (RELATIONS | RELEVANCE), (
                    f"{path.name}:{getattr(node, 'lineno', '?')} {ast.unparse(node)}"
                )


def test_relevance_members_are_chosen_only_by_the_relationship_owner():
    """A consumer may name a capability to ask about it, never a member to assign it."""
    members = _subclasses(PeerRelevance) - {PeerRelevance.__name__}
    for path, tree in _consumer_trees():
        used = _names(tree) & members
        assert not used, f"{path.name} names relevance members {sorted(used)}"


def test_peers_are_never_selected_or_ordered_by_thread_name_alone():
    """The peer list keeps the order its relations arrive in."""
    tree = ast.parse((PACKAGE / "turn_context.py").read_text())
    owners = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name in {"PeerState", "CoordinationSegment"}
    ]
    assert len(owners) == 2
    for node in (node for owner in owners for node in ast.walk(owner)):
        if isinstance(node, ast.Call):
            called = ast.unparse(node.func)
            assert called != "sorted" and not called.endswith(".sort"), (
                f"turn_context.py:{node.lineno} {ast.unparse(node)}"
            )


def test_partner_activity_only_reads():
    """A partner's goal change is shown at the next turn; it never sends, publishes or wakes."""
    tree = ast.parse((PACKAGE / "context_segments" / "partner_activity.py").read_text())
    imported = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert imported <= {
        "dataclass", "ClassVar", "FieldCodec", "GoalHistoryEntry", "GoalRevision", "GoalState",
        "Informed", "InstructionFile", "InstructionSegment", "OwnerProvenance",
    }
    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called <= {
        "grants", "goal_history", "latest", "read", "sha256", "hexdigest", "dumps", "encode",
    }


def test_relevance_reaches_only_what_a_thread_sees_and_hears():
    """Relevance grants no permission and decides no wake.

    Only the context builders read it. In particular the wake, triage,
    delivery and claim modules never do: a direct message wakes its recipient
    whoever sent it, and a reply is triaged whoever sent it, which is what
    stops two collaborators owing each other answers without end.
    """
    readers = set()
    for path, tree in _consumer_trees():
        names = _names(tree)
        imports = {
            node.module.rsplit(".", 1)[-1] for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        if "peer_relevance" in imports or names & (RELEVANCE | {"grants", "peer_relations"}):
            readers.add(path.relative_to(PACKAGE).as_posix())
    assert readers == {
        "turn_context.py", "owned_turn.py", "context_segments/partner_activity.py", "message_bus.py",
    }
