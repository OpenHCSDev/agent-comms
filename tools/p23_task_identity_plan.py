"""Prepare the nominal identity/task read cut without importing backend-bearing Thread."""

import argparse
import json
import re
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    source = args.root / "src/agent_comms"
    operations = []

    def patch(file: str, old: str, new: str) -> None:
        operations.append({"operation": "patch_target", "file_path": str(source / file),
                           "replacements": [{"old_source": old, "new_source": new}]})

    def ensure(file: str, text: str) -> None:
        operations.append({"operation": "ensure_import", "file_path": str(source / file),
                           "import_source": text})

    def remove(file: str, module: str, *names: str) -> None:
        operations.append({"operation": "remove_import_names", "file_path": str(source / file),
                           "module_name": module, "import_names": list(names)})

    identity = '''"""Identity reads shared by the original recorded and live thread declarations."""

from abc import ABC, abstractmethod

from agent_comms.registry.thread_identity import ThreadIncarnation


class ThreadIdentitySource(ABC):
    __slots__ = ()

    name: str

    @property
    @abstractmethod
    def incarnation(self) -> ThreadIncarnation: ...
'''
    task = '''"""The actual thread reads required by task emission and applicability."""

from abc import abstractmethod

from agent_comms.execution.context_owner import ContextOwner
from agent_comms.execution.turn_lease import TurnLeaseFence, TurnRecord
from agent_comms.nominal.field_codec import TurnId
from agent_comms.registry.thread_identity import ThreadRole, TurnIdentity


class TaskOwner(ContextOwner):
    __slots__ = ()

    role: type[ThreadRole]
    turn_record: TurnRecord
    turn_generation: int

    @abstractmethod
    def require_turn_lease(self) -> TurnLeaseFence: ...

    @abstractmethod
    def has_authored_turn(self, identity: TurnIdentity, turn: TurnId) -> bool: ...

    @abstractmethod
    def has_observed_task_turn(self, identity: TurnIdentity, turn: TurnId) -> bool: ...
'''
    for file, text in (("thread_identity_source.py", identity), ("task_owner.py", task)):
        operations.append({"operation": "create_file", "file_path": str(source / file), "source": text})
    patch("context_owner.py", "class ContextOwner(TaskScopeOwner[TaskScope]):",
          "class ContextOwner(TaskScopeOwner[TaskScope], ThreadIdentitySource):")
    patch("context_owner.py", "    @property\n    @abstractmethod\n    def incarnation(self) -> ThreadIncarnation: ...\n\n", "")
    remove("context_owner.py", "agent_comms.registry.thread_identity", "ThreadIncarnation")
    ensure("context_owner.py", "from agent_comms.registry.thread_identity_source import ThreadIdentitySource")
    patch("registry/threads.py", "class ThreadProvenance:", "class ThreadProvenance(ThreadIdentitySource):")
    patch("registry/threads.py", "DeclaredFamily, ContextOwner, affix=\"Thread\"", "DeclaredFamily, TaskOwner, affix=\"Thread\"")
    remove("registry/threads.py", "agent_comms.context_owner", "ContextOwner")
    ensure("registry/threads.py", "from agent_comms.registry.task_owner import TaskOwner")
    ensure("registry/threads.py", "from agent_comms.registry.thread_identity_source import ThreadIdentitySource")
    patch("registry_names.py", 'Member = TypeVar("Member", bound=ThreadProvenance, covariant=True)',
          'Member = TypeVar("Member", bound=ThreadIdentitySource, covariant=True)')
    patch("registry_names.py", "def answering_thread(self, reader: Thread) -> Thread:",
          "def answering_thread[Reader](self, reader: Reader) -> Reader:")
    narrowing = '''    def require_member[Kind: Thread](self, name: str, kind: type[Kind]) -> Kind:
        """The registered declaration, narrowed to the member an operation needs."""
        return kind.stored(self.require(name))
'''
    patch("registry_names.py", narrowing, "")
    remove("registry_names.py", "agent_comms.registry.threads", "Thread", "ThreadProvenance")
    ensure("registry_names.py", "from agent_comms.registry.thread_identity_source import ThreadIdentitySource")
    patch("registry/registry_document.py", "class RegistryPresence[StatusMap: Mapping[str, ThreadStatus]](TaskRegistry):",
          "class RegistryPresence[StatusMap: Mapping[str, ThreadStatus]](TaskRegistry[Thread]):")
    patch("registry/registry_document.py", '    """Presence reads shared by the acquired document and its detached cut."""\n\n    __slots__ = ()\n',
          '    """Presence reads shared by the acquired document and its detached cut."""\n\n    __slots__ = ()\n\n' + narrowing)
    remove("registry/registry_document.py", "__future__", "annotations")
    patch("task_registry.py", "class TaskRegistry(RegistryNames[Thread]):",
          "class TaskRegistry[Owner: TaskOwner](RegistryNames[Owner]):")
    remove("task_registry.py", "agent_comms.registry.threads", "Thread")
    ensure("task_registry.py", "from agent_comms.registry.task_owner import TaskOwner")
    tasks = source / "task_sources.py"
    before = tasks.read_text()
    after = before.replace(
        "from agent_comms.registry.threads import Thread, ThreadProvenance\n",
        "from agent_comms.registry.task_owner import TaskOwner\nfrom agent_comms.registry.thread_identity_source import ThreadIdentitySource\n",
    )
    after = re.sub(r"\bThreadProvenance\b", "ThreadIdentitySource", after)
    after = re.sub(r"\bThread\b", "TaskOwner", after)
    after = re.sub(r"\bTaskRegistry\b(?! import|\[)", "TaskRegistry[TaskOwner]", after)
    after = after.replace("import TaskRegistry[TaskOwner]", "import TaskRegistry")
    assert before != after and "from agent_comms.registry.threads" not in after
    patch("task_sources.py", before, after)
    reason = (
        "The failed cold namespace import demonstrates Thread -> AgentBackend -> pi_commands -> "
        "messages -> task_sources -> namespace. Namespace resolution needs only the original "
        "name/incarnation reads, not a backend-bearing declaration. Move ContextOwner's original "
        "incarnation contract onto ThreadIdentitySource and have the actual ThreadProvenance "
        "inherit it; retain the original cached property and every data field. TaskOwner composes "
        "ContextOwner with the exact turn/role reads already implemented by Thread; Thread "
        "inherits it with no forwarding bodies or state. TaskRegistry keeps its concrete member "
        "as a type parameter, supplied by RegistryPresence[Thread]. Only live RegistryPresence "
        "consumers perform member narrowing; the complete src/tests/paired-Toad search finds "
        "no recorded-provenance caller. Move the exact require_member implementation to that "
        "existing live owner, preserving lookup/effect/refusal order. This is not a claim that "
        "moving a method reduces an implementation-pair count. Explicitly use native Python "
        "3.14 annotation scopes in registry_document so moved Kind and original StatusMap remain "
        "resolvable. DeletedThread's reader return is the identical opaque argument, now named "
        "by a native method type parameter. These are authored ownership and annotation decisions; "
        "exact-patch preflight does not certify their behavioral equivalence. Require cold roots, "
        "actual provider/MRO checks, original field/codec identity and sender/task/namespace consumers. "
        "Guarded callbacks elsewhere are still OPEN; this plan does not claim complete P23 delivery."
    )
    document = {"recipes": [{"recipe_id": "p23-original-identity-and-task-owner-roles", "reason": reason,
                             "operations": operations, "architecture_guards": [], "authority_claims": []}],
                "architecture_guards": []}
    args.plan.write_text(json.dumps(document, indent=2) + "\n")
    print(json.dumps({"operations": len(operations)}))


if __name__ == "__main__":
    main()
