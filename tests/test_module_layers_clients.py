"""A stack client's imports root reachability and fail on modules Core lacks."""

import ast
from collections.abc import Callable
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import tomli_w

from module_layers import MaterializedClients, OutgoingWire, Package, StackClient
from agent_comms_project import AgentCommsProject, ClientTypeErrors
from agent_comms.head_review import BaseSide, HeadSide, IntegrationInput, Side
from agent_comms.todos import DraftListedPullRequest
from github_fixture import GitHubFixture
from integration_source_fixture import MutualSources

pytest_plugins = ("github_fixture", "test_holder_standing_rules", "integration_source_fixture")


def write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.mark.parametrize("declaration, roots", (
    ("", ("src", ".")),
    ('mypy_path = ["src", "tools"]', ("src", "tools")),
    ('mypy_path = "src, tools"', ("src", "tools")),
    ("mypy_path = []", ()),
    ('mypy_path = ["{external}", "src"]', ("{external}", "src")),
))
def test_client_roots_follow_each_projects_native_declaration(
    tmp_path: Path, declaration: str, roots: tuple[str, ...],
) -> None:
    external = tmp_path / "external-library"
    external.mkdir()
    client = tmp_path / "client"
    dependency = tmp_path / "dependency"
    write(client / "pyproject.toml", '[tool.mypy]\n' + declaration.format(external=external))
    write(dependency / "pyproject.toml", '[tool.mypy]\nmypy_path = ["lib"]\n')
    acquired = StackClient("client", client, dependencies=(dependency,))
    expected = tuple(str((client / path.format(external=external)).resolve()) for path in roots)
    assert acquired.source_paths == expected
    assert acquired.import_paths == (*expected, str(dependency / "lib"))


def test_materialized_clients_check_matched_sources_and_both_mixed_sources_fail(
    mutual_sources: MutualSources, github_cli: GitHubFixture,
    materialized_clients: Callable[[Path, IntegrationInput], MaterializedClients],
) -> None:
    pair = mutual_sources
    inputs = pair.capture(("P2", "P3"), "materialized module clients")
    core = inputs.source(pair.core.github)
    base = BaseSide.require_materialized(pair.output, core)
    head = HeadSide.require_materialized(pair.output, core)
    files = tuple(sorted(pair.output.rglob(".git")))
    resource = materialized_clients(pair.output, inputs)
    base_clients = resource.cuts[BaseSide]
    head_clients = resource.cuts[HeadSide]
    assert tuple(sorted(pair.output.rglob(".git"))) == files  # No later fresh main clone.
    assert Package(base, base_clients).missing_names == ()
    assert Package(head, head_clients).missing_names == ()
    assert {name for entry, name in Package(base, head_clients).missing_names} == {"new_api"}
    assert {name for entry, name in Package(head, base_clients).missing_names} == {"old_api"}
    assert not any(call[:2] == ("pr", "merge") for call in github_cli.calls())


@pytest.mark.parametrize("baseline_has_client,candidate_has_client", ((False, True), (True, False)))
def test_actual_client_membership_stays_absent_on_its_own_cut(
    mutual_sources: MutualSources, github_cli: GitHubFixture,
    baseline_has_client: bool, candidate_has_client: bool,
    materialized_clients: Callable[[Path, IntegrationInput], MaterializedClients],
) -> None:
    pair = mutual_sources
    full = (pair.core.checkout / "stack/pyproject.toml").read_text()
    absent = tomllib.loads(full)
    absent["project"]["dependencies"].remove("source-client")
    del absent["tool"]["uv"]["sources"]["source-client"]
    absent_text = tomli_w.dumps(absent)
    if not baseline_has_client:
        pair.core.git(pair.core.checkout, "switch", "main")
        pair.core.commit_change("Baseline has no client declaration", *pair.core.write_files({
            Path("stack/pyproject.toml"): absent_text}))
        pair.core.git(pair.core.checkout, "push", "origin", "main")
        pair.core.git(pair.core.checkout, "switch", "feature")
        pair.core.git(pair.core.checkout, "merge", "--no-edit", "main")
    head = pair.core.publish_change("Declare only the candidate's actual client membership", *pair.core.write_files({
        Path("stack/pyproject.toml"): full if candidate_has_client else absent_text}))
    stamp = "2026-10-24T00:00:00Z"
    github_cli.put(pair.core.github, DraftListedPullRequest(
        pair.core.number, "feature", head, stamp, "main", reviewable_times=(stamp,)))
    inputs = pair.capture(("P2", "P3"), "actual added and removed client")
    resource = materialized_clients(pair.output, inputs)
    core = inputs.source(pair.core.github)
    tested = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    for side, present in ((BaseSide, baseline_has_client), (HeadSide, candidate_has_client)):
        clients = resource.at(pair.core.checkout, core.baseline.commit, tested, side)
        assert len(clients) == int(present)
        assert all(client.checkout == side.checkout(pair.output, pair.client.github) for client in clients)
    assert len(ClientTypeErrors.parts(AgentCommsProject(pair.core.checkout, clients=resource),
                                     core.baseline.commit, tested,
                                     {"src/agent_comms/api.py"}, "src/")) == 1


def test_materialized_client_files_come_from_each_actual_cut(
    mutual_sources: MutualSources, github_cli: GitHubFixture,
    materialized_clients: Callable[[Path, IntegrationInput], MaterializedClients],
) -> None:
    pair = mutual_sources
    pair.client.git(pair.client.checkout, "mv", "src/workflow.py", "src/replacement.py")
    commit = pair.client.publish_change("Move the candidate client file", "src")
    stamp = "2026-10-24T00:00:00Z"
    github_cli.put(pair.client.github, DraftListedPullRequest(
        pair.client.number, "feature", commit, stamp, "main", reviewable_times=(stamp,)))
    inputs = pair.capture(("P2", "P3"), "side-specific client files")
    cuts: dict[type[Side], set[str]] = {}
    resource = materialized_clients(pair.output, inputs)
    for side in (BaseSide, HeadSide):
        client, = resource.cuts[side]
        cuts[side] = {entry.path for entry in client.imports}
    assert cuts[BaseSide] == {"src/workflow.py"}
    assert cuts[HeadSide] == {"src/replacement.py"}


@pytest.mark.parametrize("name", ("source-client", "source-renamed"))
def test_materialized_selection_includes_peer_imports_but_not_absent_paths(
    mutual_sources: MutualSources, github_cli: GitHubFixture, name: str,
    materialized_clients: Callable[[Path, IntegrationInput], MaterializedClients],
) -> None:
    pair = mutual_sources
    client_project = tomllib.loads((pair.client.checkout / "pyproject.toml").read_text())
    client_project["project"]["name"] = name
    files = pair.client.write_files({
        Path("src/workflow.py"): 'def run() -> str:\n    return "local"\n',
        Path("pyproject.toml"): tomli_w.dumps(client_project)})
    commit = pair.client.publish_change("Keep the file while removing its candidate Core import", *files)
    stack = tomllib.loads((pair.core.checkout / "stack/pyproject.toml").read_text())
    stack["project"]["dependencies"] = [name if item == "source-client" else item
                                         for item in stack["project"]["dependencies"]]
    stack["tool"]["uv"]["sources"][name] = stack["tool"]["uv"]["sources"].pop("source-client")
    core_head = pair.core.publish_change("Declare the actual candidate client name", *pair.core.write_files({
        Path("stack/pyproject.toml"): tomli_w.dumps(stack)}))
    stamp = "2026-10-24T00:00:00Z"
    github_cli.put(pair.core.github, DraftListedPullRequest(
        pair.core.number, "feature", core_head, stamp, "main", reviewable_times=(stamp,)))
    github_cli.put(pair.client.github, DraftListedPullRequest(
        pair.client.number, "feature", commit, stamp, "main", reviewable_times=(stamp,)))
    inputs = pair.capture(("P2", "P3"), "selected file union")
    resource = materialized_clients(pair.output, inputs)
    core = inputs.source(pair.core.github)
    head = pair.core.git(pair.checkout.path, "rev-parse", "HEAD").strip()
    for side in (BaseSide, HeadSide):
        client, = resource.at(pair.core.checkout, core.baseline.commit, head, side)
        assert client.files_importing("agent_comms") == ["src/workflow.py"]
        assert client.checkout == side.checkout(pair.output, pair.client.github)
    candidate, = resource.at(pair.core.checkout, core.baseline.commit, head, HeadSide)
    assert candidate.imports == ()


def test_client_imports_root_reachability_and_name_missing_modules(tmp_path):
    core = tmp_path / "core"
    write(
        core / "pyproject.toml",
        '[project.scripts]\nac = "agent_comms.cli:main"\nguard = "agent_comms_hooks.guard:main"\n',
    )
    write(core / "src/agent_comms/__init__.py")
    write(core / "src/agent_comms/cli.py")
    write(core / "src/agent_comms/ingress.py")
    write(core / "src/agent_comms/orphan.py")
    write(core / "src/agent_comms/rules.py")
    write(core / "src/agent_comms_hooks/guard.py", "from agent_comms_hooks import shared\n")
    write(core / "src/agent_comms_hooks/shared.py", "from agent_comms.rules import Rule\n")
    client = tmp_path / "toad"
    write(
        client / "src/toad/session.py",
        "from agent_comms.ingress import Ingress\nimport agent_comms.deleted\n"
        "import os\nimport agent_comms_hooks.watch\nimport agent_comms\n",
    )

    package = Package(core, (StackClient("toad", client),))

    assert [str(entry) for entry in package.missing] == [
        "toad:src/toad/session.py:2 imports agent_comms.deleted"
    ]
    assert package.client_surface == {"agent_comms.ingress": {"toad"}}
    assert package.unreached == ("agent_comms.orphan",)


def test_client_names_must_be_defined_by_their_module(tmp_path):
    core = tmp_path / "core"
    write(core / "pyproject.toml", '[project.scripts]\nac = "agent_comms.cli:main"\n')
    write(core / "src/agent_comms/__init__.py")
    write(core / "src/agent_comms/cli.py")
    write(
        core / "src/agent_comms/routes.py",
        "from os import environ as env\n"
        "class Route: ...\n"
        "if True:\n    def current(): ...\n"
        "DEFAULT, OTHER = 1, 2\n",
    )
    client = tmp_path / "toad"
    write(
        client / "tests/test_routes.py",
        "from agent_comms.routes import Route, current, env, OTHER, implicit\n"
        "from agent_comms.routes import *\n"
        "from agent_comms import routes\n",
    )

    package = Package(core, (StackClient("toad", client),))

    assert [(str(entry), name) for entry, name in package.missing_names] == [
        ("toad:tests/test_routes.py:1 imports agent_comms.routes", "implicit")
    ]


def wire_base(tmp_path: Path, wire: OutgoingWire) -> Path:
    """A base tree holding only the modules the outgoing wire names, copied from this tree."""
    base = tmp_path / "base"
    write(base / "pyproject.toml", '[project.scripts]\nac = "agent_comms.cli:main"\n')
    write(base / "src/agent_comms/__init__.py")
    for entry in wire.imports:
        write(base / entry.path, (wire.checkout / entry.path).read_text())
    return base


def test_crosses_runtimes_matches_the_tree_that_declares_it(tmp_path: Path) -> None:
    wire = OutgoingWire.at()

    package = Package(wire_base(tmp_path, wire), (wire,))

    assert (package.missing, package.missing_names, package.drifted) == ((), (), ())


def test_crosses_runtimes_fails_on_a_renamed_session_field(tmp_path: Path) -> None:
    from agent_comms.cli_bridges.monitor import PauseSession

    wire = OutgoingWire.at()
    base = wire_base(tmp_path, wire)
    session, = (entry for entry in wire.imports if entry.names == (PauseSession.__name__,))
    path = base / session.path
    tree = ast.parse(path.read_text())
    declaration, = (node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == PauseSession.__name__)
    field, = (node for node in declaration.body if isinstance(node, ast.AnnAssign))
    field.target = ast.Name(id="renamed_process", ctx=ast.Store())
    path.write_text(ast.unparse(tree))

    package = Package(base, (wire,))

    assert (package.missing, package.missing_names) == ((), ())
    assert [drift for _, drift in package.drifted] == [
        "PauseSession declares (renamed_process: ProcessIdentity), sent as (process: ProcessIdentity)"
    ]


def test_crosses_runtimes_missing_file_and_class_share_one_verdict(tmp_path: Path) -> None:
    from agent_comms.cli_bridges.monitor import PauseSession

    wire = OutgoingWire.at()
    base = wire_base(tmp_path, wire)
    session, = (entry for entry in wire.imports if entry.names == (PauseSession.__name__,))
    path = base / session.path
    tree = ast.parse(path.read_text())
    tree.body = [node for node in tree.body
                 if not (isinstance(node, ast.ClassDef) and node.name == PauseSession.__name__)]
    path.write_text(ast.unparse(tree))
    missing_class = tuple(problem for entry, problem in Package(base, (wire,)).drifted
                          if entry.names == session.names)
    path.unlink()
    missing_file = tuple(problem for entry, problem in Package(base, (wire,)).drifted
                         if entry.names == session.names)

    assert missing_file == missing_class == (
        "PauseSession is missing; sent fields (process: ProcessIdentity)",
    )


def test_crosses_runtimes_discovers_sessions_without_importing_monitor(tmp_path: Path) -> None:
    subprocess.run(
        (sys.executable, "-c",
         "import sys; from agent_comms.installer.runtime_wire import CrossesRuntimes; "
         "assert 'agent_comms.cli_bridges.monitor' not in sys.modules; "
         "assert any(entry.names == ('PauseSession',) for entry in CrossesRuntimes.declarations())"),
        check=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(OutgoingWire.at().checkout / "src")},
    )


def test_crosses_runtimes_discovers_a_new_session_member(tmp_path: Path) -> None:
    source = tmp_path / "src"
    shutil.copytree(OutgoingWire.at().checkout / "src/agent_comms", source / "agent_comms",
                    ignore=shutil.ignore_patterns("__pycache__", ".git"))
    script = source / "agent_comms/new_session.py"
    write(script, '''
from dataclasses import dataclass
from agent_comms.cli_bridges.monitor import SessionRequest, SessionReady
from agent_comms.installer.runtime_wire import CrossesRuntimes

@dataclass(frozen=True)
class FreshSession(CrossesRuntimes, SessionRequest[SessionReady], declared_name="fresh"):
    fresh_field: str

    async def handle(self, service, peer):
        return SessionReady()
''')
    subprocess.run(
        (sys.executable, "-c",
         "from agent_comms.installer.runtime_wire import CrossesRuntimes; "
         "entry, = (entry for entry in CrossesRuntimes.declarations() if entry.names == ('FreshSession',)); "
         "assert entry.fields == ('fresh_field: str',)"), check=True,
        env={**os.environ, "PYTHONPATH": str(source)},
        timeout=60,
    )
