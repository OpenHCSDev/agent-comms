"""Move flat agent_comms modules into a subpackage and rewrite every reference.

Usage: python tools/move_flat_modules.py REPO PACKAGE MODULE[:NEW]...
MODULE is a flat module or a package directory; NEW renames it inside PACKAGE
(coordination_tables:tables). No shims: the old module paths stop existing.
Rehearsed for PackageStoragePhase: every agent_comms module imports after the move.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

repo, package, *specs = sys.argv[1:]
root = Path(repo)
src = root / "src" / "agent_comms"
dest = src / package
NEW = dict((spec.split(":") + spec.split(":"))[:2] for spec in specs)
modules = list(NEW)
names = "|".join(modules)


def source(module: str) -> Path:
    """The flat module file, or the package directory when the module is one."""
    return src / module if (src / module).is_dir() else src / f"{module}.py"


# A consumer repository (Toad, Textual) declares none of the modules: rewrite its references only.
owns = any(source(module).exists() for module in modules)
if owns:
    dest.mkdir(exist_ok=True)
    (dest / "__init__.py").touch()
DIRECTORIES = {module for module in modules if owns and source(module).is_dir()}
for module in modules if owns else ():
    old = source(module)
    new = dest / NEW[module] if module in DIRECTORIES else dest / f"{NEW[module]}.py"
    subprocess.run(["git", "-C", repo, "mv", str(old.relative_to(root)), str(new.relative_to(root))],
                   check=True)


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
            target = ["agent_comms", package, NEW[target[1]], *target[2:]]
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
        comment = "  ".join(re.findall(r"#[^\n]*", listed))
        listed = re.sub(r"#[^\n]*", "", listed)
        names = [n.strip() for n in listed.strip().strip("()").replace("\n", " ").split(",") if n.strip()]
        moved = [n for n in names if n.split()[0] in MOVED]
        if not moved:
            return match[0]
        rest = [n for n in names if n not in moved]
        moved = [n if NEW[n.split()[0]] == n.split()[0] else
                 " ".join([NEW[n.split()[0]], *n.split()[1:]] if " as " in n
                          else [NEW[n], "as", n]) for n in moved]
        home = spec if not spec.startswith(".") else relative(path_parts, ["agent_comms"])
        destination = (f"agent_comms.{package}" if not spec.startswith(".")
                       else relative(path_parts, ["agent_comms", package]))
        lines = [f"{indent}from {home} import {', '.join(rest)}"] if rest else []
        lines.append(f"{indent}from {destination} import {', '.join(moved)}")
        if comment:
            lines[0] += f"  {comment}"
        return "\n".join(lines) + ("\n" if match[0].endswith("\n") else "")

    return IMPORT_LIST.sub(fix, text)
ABSOLUTE = re.compile(rf"\bagent_comms\.({names})\b")
SLASHED = re.compile(rf"\bsrc/agent_comms/({names})(\.py\b|/)")
ABSENT = re.compile(r"PathsAbsent\(\((?:[^()]|\([^()]*\))*\)\)")
QUOTED_BARE = re.compile(rf"\"({names})(\.py\"|/)")

ROOTS = ("src", "tests", "tools", "stack", "benchmarks", "experiments", "scripts", "extensions")
for module in MOVED if owns else ():
    # A module that finds its siblings from its own position moves to a new set of siblings.
    moved = dest / NEW[module]
    for file in sorted(moved.rglob("*.py")) if module in DIRECTORIES else (moved.with_suffix(".py"),):
        for number, line in enumerate(file.read_text().splitlines(), 1):
            if re.search(r"__package__|__file__|__name__\)", line):
                print(f"SELF-LOCATING {file.relative_to(src)}:{number}: {line.strip()}")


def old_location(path: Path) -> list[str]:
    """The package parts a file had before this move, so its relative imports resolve as written."""
    parts = package_depth(path)
    if not owns or parts[:2] != ["agent_comms", package]:
        return parts
    if path.parent == dest and path.stem in {NEW[m] for m in MOVED - DIRECTORIES}:
        return ["agent_comms"]
    for module in DIRECTORIES:
        if parts[2:3] == [NEW[module]]:
            return ["agent_comms", module, *parts[3:]]
    return parts


for path in sorted({*(p for d in ROOTS for p in (root / d).rglob("*.py")),
                    *(p for p in (root / "pyproject.toml",) if p.exists())}):
    text = path.read_text()
    original = text
    if path.is_relative_to(src):
        text = rewrite_relative(path, text, old_location(path))
        text = split_moved_names(package_depth(path), text)
    text = ABSOLUTE.sub(lambda m: f"agent_comms.{package}.{NEW[m[1]]}", text)
    # A PathsAbsent claim names the old paths on purpose: they are what must stay deleted.
    claims = ABSENT.findall(text)
    text = SLASHED.sub(lambda m: f"src/agent_comms/{package}/{NEW[m[1]]}{m[2]}", text)
    for claim, rewritten in zip(claims, ABSENT.findall(text), strict=True):
        text = text.replace(rewritten, claim, 1)
    if not path.is_relative_to(src):
        text = split_moved_names(["outside"], text)
    if path.name == "plan_phases.py":
        text = QUOTED_BARE.sub(lambda m: f'"{package}/{NEW[m[1]]}{m[2]}', text)
    if text != original:
        path.write_text(text)
