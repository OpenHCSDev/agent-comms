"""Replay the original Toad manifest using the original provider Git history."""
from __future__ import annotations

import difflib
import json
import sys
import tomllib
from importlib.metadata import EntryPoint, EntryPoints
from pathlib import Path

from nominal_refactor_advisor.ast_tools import parse_python_modules
from nominal_refactor_advisor.audit.repository import Repository
from nominal_refactor_advisor.codemod import CodemodSourceSnapshot, RefactorRecipe, ReplayGitModuleImportsOperation

provider_path, caller_path, before_ref, after_ref, caller_ref, private_path = sys.argv[1:]
private = Path(private_path)
if private.exists():
    raise RuntimeError(f"Refusing to repeat an existing private run: {private}")
private.mkdir(parents=True)
caller = Repository(Path(caller_path))
caller.extract(caller_ref, private, "pyproject.toml", "src/toad/__init__.py")
manifest = private / "pyproject.toml"
before = manifest.read_text()
operation = ReplayGitModuleImportsOperation(
    provider_repository=provider_path, source_revision=before_ref,
    target_revision=after_ref, caller_root=str(private),
)
snapshot = CodemodSourceSnapshot.from_modules(parse_python_modules(private / "src"))
result = RefactorRecipe("repair-original-preparation-group").with_operation(operation).simulate(snapshot)
assert result.is_clean, result
result.apply()
after = manifest.read_text()
old_groups = tomllib.loads(before)["project"]["entry-points"]
new_groups = tomllib.loads(after)["project"]["entry-points"]
removed = set(old_groups) - set(new_groups)
added = set(new_groups) - set(old_groups)
assert len(removed) == len(added) == 1, (removed, added)
old_group, = removed
new_group, = added
assert old_groups[old_group] == new_groups[new_group] == {"neovim": "toad.neovim_runtime:Neovim"}
assert after == before.replace(f'"{old_group}"', f'"{new_group}"', 1)
entries = EntryPoints(EntryPoint(name=name, value=value, group=group)
                     for group, members in new_groups.items() for name, value in members.items())
assert len(entries.select(group=new_group)) == 1
assert entries.select(group=new_group)["neovim"].value == "toad.neovim_runtime:Neovim"
app_imports = sorted(name for name in sys.modules if name == "toad" or name.startswith("toad.")
                     or name == "agent_comms" or name.startswith("agent_comms."))
assert app_imports == [], app_imports
print(json.dumps({
    "old_group": old_group, "new_group": new_group,
    "hook_target": entries.select(group=new_group)["neovim"].value,
    "selected_hook_count": len(entries.select(group=new_group)),
    "app_imports": app_imports, "caller": caller.revision(caller_ref),
    "provider_source": operation.source_revision, "provider_target": operation.target_revision,
    "python_source_changes": sorted(result.simulation.rewritten_sources),
    "diff": "".join(difflib.unified_diff(before.splitlines(keepends=True),
        after.splitlines(keepends=True), fromfile="original/pyproject.toml", tofile="replayed/pyproject.toml")),
}, indent=2))
