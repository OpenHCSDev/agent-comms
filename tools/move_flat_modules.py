"""Move flat agent_comms modules into a subpackage and rewrite every reference.

Usage: python tools/move_flat_modules.py REPO PACKAGE MODULE...
No shims: the old module paths stop existing. Rehearsed for PackageStoragePhase:
every agent_comms module imports after the storage move.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

repo, package, *modules = sys.argv[1:]
root = Path(repo)
src = root / "src" / "agent_comms"
dest = src / package
names = "|".join(modules)

# A consumer repository (Toad, Textual) declares none of the modules: rewrite its references only.
owns = (src / f"{modules[0]}.py").exists()
if owns:
    dest.mkdir(exist_ok=True)
    (dest / "__init__.py").touch()
for module in modules if owns else ():
    subprocess.run(["git", "-C", repo, "mv", f"src/agent_comms/{module}.py",
                    f"src/agent_comms/{package}/{module}.py"], check=True)


def package_depth(path: Path) -> list[str]:
    """Package parts of a module under src/agent_comms, e.g. ['agent_comms', 'team']."""
    return list(path.relative_to(root / "src").parent.parts)


def resolve(parts: list[str], dots: int, rest: str) -> list[str]:
    base = parts[: len(parts) - (dots - 1)]
    return base + ([p for p in rest.split(".") if p] if rest else [])


def relative(from_parts: list[str], target: list[str]) -> str:
    common = 0
    while common < min(len(from_parts), len(target)) and from_parts[common] == target[common]:
        common += 1
    dots = "." * (len(from_parts) - common + 1)
    return dots + ".".join(target[common:])


RELATIVE = re.compile(r"(?m)^(\s*from\s+)(\.+)([\w.]*)(\s+import\b)")
MOVED = set(modules)


def rewrite_relative(path: Path, text: str, old_parts: list[str]) -> str:
    new_parts = package_depth(path)

    def fix(match: re.Match[str]) -> str:
        target = resolve(old_parts, len(match[2]), match[3])
        if len(target) >= 2 and target[0] == "agent_comms" and target[1] in MOVED:
            target = ["agent_comms", package, *target[1:]]
        if target == new_parts and not match[3]:
            return match[0]
        return f"{match[1]}{relative(new_parts, target)}{match[4]}"

    return RELATIVE.sub(fix, text)


IMPORT_LIST = re.compile(r"(?m)^([ \t]*)from[ \t]+(\.+|agent_comms)[ \t]+import[ \t]+(\([^)]*\)|[^\n#]+)")


def split_moved_names(path_parts: list[str], text: str) -> str:
    """`from agent_comms import a, store_files` or `from . import store_files`: the moved names
    leave for the package, the rest stay, each list resolved from where the file now lives."""

    def fix(match: re.Match[str]) -> str:
        indent, spec, listed = match[1], match[2], match[3]
        base = resolve(path_parts, len(spec), "") if spec.startswith(".") else ["agent_comms"]
        if base != ["agent_comms"]:
            return match[0]
        names = [n.strip() for n in listed.strip().strip("()").replace("\n", " ").split(",") if n.strip()]
        moved = [n for n in names if n.split()[0] in MOVED]
        if not moved:
            return match[0]
        rest = [n for n in names if n not in moved]
        home = spec if not spec.startswith(".") else relative(path_parts, ["agent_comms"])
        destination = (f"agent_comms.{package}" if not spec.startswith(".")
                       else relative(path_parts, ["agent_comms", package]))
        lines = [f"{indent}from {home} import {', '.join(rest)}"] if rest else []
        lines.append(f"{indent}from {destination} import {', '.join(moved)}")
        return "\n".join(lines) + ("\n" if match[0].endswith("\n") else "")

    return IMPORT_LIST.sub(fix, text)
ABSOLUTE = re.compile(rf"\bagent_comms\.({names})\b")
SLASHED = re.compile(rf"\bsrc/agent_comms/({names})\.py\b")
QUOTED_BARE = re.compile(rf"\"({names})\.py\"")

for path in sorted({*(p for d in ("src", "tests", "tools", "stack") for p in (root / d).rglob("*.py")),
                    *(p for p in (root / "pyproject.toml",) if p.exists())}):
    text = path.read_text()
    original = text
    if path.is_relative_to(src):
        moved_now = path.parent == dest and path.stem in MOVED
        old_parts = ["agent_comms"] if moved_now else package_depth(path)
        text = rewrite_relative(path, text, old_parts)
        text = split_moved_names(package_depth(path), text)
    text = ABSOLUTE.sub(rf"agent_comms.{package}.\1", text)
    text = SLASHED.sub(rf"src/agent_comms/{package}/\1.py", text)
    if not path.is_relative_to(src):
        text = split_moved_names(["outside"], text)
    if path.name == "plan_phases.py":
        text = QUOTED_BARE.sub(rf'"{package}/\1.py"', text)
    if text != original:
        path.write_text(text)
