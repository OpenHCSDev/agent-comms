"""Source acquisition graph used by the store-lock order regression."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from agent_comms.pinned_audit import ParsedModule
from agent_comms.store_files import StoreLockOwner


@dataclass(frozen=True, order=True)
class Acquisition:
    store: str
    site: str
    via: str
    expression: str


class StoreLockGraph:
    """Resolve lexical and owner-call acquisitions, preserving context-manager yields.

    Source annotations and constructor assignments supply owners. Analysis expands the
    immediate called owner plus its lock wrappers; arbitrary dynamic calls are outside
    this boundary. Reentry in these captured paths uses the same registry root, so the
    original held acquisition, rather than its intervening catalog, is the lender.
    """

    def __init__(self, root: Path):
        self.root = root
        self.modules = []
        for path in sorted((self.root / "src").rglob("*.py")):
            if "skills" in path.parts:
                continue
            text = path.read_text()
            self.modules.append(
                ParsedModule(str(path.relative_to(self.root)), text, ast.parse(text), str(path))
            )
        self.classes = {}
        self.functions = {}
        self.contexts = {}
        self.outer_functions = {}
        for module in self.modules:
            for node in ast.walk(module.tree):
                if isinstance(node, ast.ClassDef):
                    self.classes.setdefault(node.name, []).append((module, node))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.functions.setdefault(node.name, []).append((module, node))
            parents = {
                child: parent
                for parent in ast.walk(module.tree)
                for child in ast.iter_child_nodes(parent)
            }
            for node in ast.walk(module.tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    enclosing = parents.get(node)
                    while enclosing is not None and (
                        not isinstance(enclosing, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ):
                        enclosing = parents.get(enclosing)
                    if enclosing is not None:
                        self.outer_functions[id(node)] = enclosing
                    parent = parents.get(node)
                    while parent is not None and (not isinstance(parent, ast.ClassDef)):
                        parent = parents.get(parent)
                    self.contexts[id(node)] = (module, parent.name if parent else "")
        self.attrs = {}
        for _ in range(8):
            changed = False
            for name in self.classes:
                found = self.declaration(name)
                if not found:
                    continue
                module, cls = found
                for node in cls.body:
                    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                        key = (name, node.target.id)
                        value = self.annotation(node.annotation)
                        if value and self.attrs.get(key) != value:
                            self.attrs[key] = value
                            changed = True
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        env = {
                            arg.arg: self.annotation(arg.annotation)
                            for arg in (
                                *node.args.posonlyargs,
                                *node.args.args,
                                *node.args.kwonlyargs,
                            )
                        }
                        env.update(self=name, cls=name)
                        for sub in ast.walk(node):
                            if isinstance(sub, ast.Assign):
                                if (
                                    len(sub.targets) == 1
                                    and isinstance(sub.targets[0], ast.Tuple)
                                    and isinstance(sub.value, ast.Tuple)
                                ):
                                    for target, value_node in zip(
                                        sub.targets[0].elts, sub.value.elts, strict=True
                                    ):
                                        if (
                                            isinstance(value_node, ast.Name)
                                            and isinstance(target, ast.Attribute)
                                            and isinstance(target.value, ast.Name)
                                            and (target.value.id == "self")
                                        ):
                                            value_type = env.get(value_node.id)
                                            if value_type:
                                                self.attrs[name, target.attr] = value_type
                                value = None
                                if isinstance(sub.value, ast.Name):
                                    value = env.get(sub.value.id)
                                elif isinstance(sub.value, ast.Call):
                                    call = sub.value.func
                                    if isinstance(call, ast.Name) and self.declaration(call.id):
                                        value = call.id
                                    elif (
                                        isinstance(call, ast.Attribute)
                                        and isinstance(call.value, ast.Name)
                                        and (call.attr == "at")
                                        and self.declaration(call.value.id)
                                    ):
                                        value = call.value.id
                                elif isinstance(sub.value, ast.Attribute) and isinstance(
                                    sub.value.value, ast.Name
                                ):
                                    value = self.attrs.get(
                                        (env.get(sub.value.value.id, ""), sub.value.attr)
                                    )
                                if value:
                                    for target in sub.targets:
                                        if isinstance(target, ast.Name):
                                            env[target.id] = value
                                        elif (
                                            isinstance(target, ast.Attribute)
                                            and isinstance(target.value, ast.Name)
                                            and (target.value.id == "self")
                                        ):
                                            key = (name, target.attr)
                                            if self.attrs.get(key) != value:
                                                self.attrs[key] = value
                                                changed = True
            if not changed:
                break
        self.unresolved = set()
        self.all_sites = set()
        self.pairs = set()
        self.ranks = {}
        for name in self.classes:
            inherited = [
                tier.lock_rank()
                for tier in StoreLockOwner.lock_tiers()
                if self.derives(name, tier.__name__)
            ]
            if len(inherited) == 1:
                self.ranks[name] = inherited[0]
        for module in self.modules:
            for node in ast.walk(module.tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = self.contexts[id(node)][1]
                    self.walk(node.body, [], self.environment(node, owner), module)

    def annotation(self, node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                return self.annotation(ast.parse(node.value, mode="eval").body)
            except SyntaxError:
                return ""
        if isinstance(node, ast.Subscript):
            return self.annotation(node.value)
        return ""

    def declaration(self, name):
        values = self.classes.get(name, ())
        return values[0] if len(values) == 1 else None

    def bases(self, name):
        found = self.declaration(name)
        return [self.annotation(base) for base in found[1].bases] if found else []

    def derives(self, name, root, seen=frozenset()):
        return name == root or (
            name not in seen
            and any(self.derives(base, root, seen | {name}) for base in self.bases(name))
        )

    def attribute(self, owner, name, seen=frozenset()):
        if owner in seen:
            return ""
        if (owner, name) in self.attrs:
            return self.attrs[owner, name]
        for base in self.bases(owner):
            if value := self.attribute(base, name, seen | {owner}):
                return value
        return ""

    def method(self, owner, name, seen=frozenset()):
        if owner in seen:
            return None
        found = self.declaration(owner)
        if not found:
            return None
        for node in found[1].body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return (found[0], node)
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id in {"changing", "reading"}
            ):
                wrapper = self.method(owner, node.value.func.id)
                if wrapper:
                    nested = next(n for n in wrapper[1].body if isinstance(n, ast.FunctionDef))
                    return (found[0], nested)
        for base in self.bases(owner):
            if found := self.method(base, name, seen | {owner}):
                return found
        return None

    def typeof(self, node, env):
        if isinstance(node, ast.Name):
            return env.get(node.id, node.id if self.declaration(node.id) else "")
        if isinstance(node, ast.Attribute):
            return self.attribute(self.typeof(node.value, env), node.attr)
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if self.declaration(node.func.id):
                    return node.func.id
                candidates = self.functions.get(node.func.id, ())
                if len(candidates) == 1:
                    return self.annotation(candidates[0][1].returns)
            elif isinstance(node.func, ast.Attribute):
                owner = self.typeof(node.func.value, env)
                if node.func.attr == "at" and self.declaration(owner):
                    return owner
                found = self.method(owner, node.func.attr)
                if found:
                    answer = self.annotation(found[1].returns)
                    return owner if answer in {"Self", "Iterator", "AsyncIterator"} else answer
        return ""

    def environment(self, node, owner, supplied=None):
        env = {
            arg.arg: self.annotation(arg.annotation)
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        }
        env.update(self=owner, cls=owner)
        env.update({key: value for key, value in (supplied or {}).items() if value})
        if id(node) in self.outer_functions:
            for key, value in self.environment(self.outer_functions[id(node)], owner).items():
                if not env.get(key):
                    env[key] = value
        env["__function__"] = node
        for _ in range(3):
            for item in ast.walk(node):
                if isinstance(item, ast.Assign):
                    if (
                        len(item.targets) == 1
                        and isinstance(item.targets[0], ast.Tuple)
                        and isinstance(item.value, ast.Tuple)
                    ):
                        for target, value_node in zip(
                            item.targets[0].elts, item.value.elts, strict=True
                        ):
                            if isinstance(target, ast.Name) and (
                                value_type := self.typeof(value_node, env)
                            ):
                                env[target.id] = value_type
                    value = self.typeof(item.value, env)
                    if value:
                        for target in item.targets:
                            if isinstance(target, ast.Name):
                                env[target.id] = value
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    env[item.target.id] = self.annotation(item.annotation)
        return env

    def iteration_types(self, expr, env, seen=frozenset()):
        if id(expr) in seen:
            return ()
        seen = seen | {id(expr)}
        if isinstance(expr, (ast.Tuple, ast.List)):
            return tuple(value for element in expr.elts if (value := self.typeof(element, env)))
        if isinstance(expr, ast.Name):
            for node in ast.walk(env["__function__"]):
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == expr.id for t in node.targets
                ):
                    return self.iteration_types(node.value, env, seen)
        if isinstance(expr, ast.Attribute):
            owner = self.typeof(expr.value, env)
            found = self.method(owner, expr.attr)
            if found:
                getter = found[1]
                local = self.environment(getter, owner)
                for node in ast.walk(getter):
                    if isinstance(node, ast.Return):
                        values = self.iteration_types(node.value, local, seen)
                        if values:
                            return values
        return ()

    def locks(self, call, env, module, budget=1, trail=()):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            return ()
        owner = self.typeof(call.func.value, env)
        name = call.func.attr
        site = f"{module.path}:{call.lineno}"
        if name in {"locked", "locked_async"}:
            self.all_sites.add((site, owner, ast.unparse(call)))
            if self.derives(owner, "StoreLockOwner") or owner == "RestartQueue":
                return (Acquisition(owner, site, site, ast.unparse(call.func.value)),)
            if owner not in {"StoreLockAcquisition", "UnacquiredStoreLock"}:
                self.unresolved.add((site, ast.unparse(call), owner))
            return ()
        if (
            name == "ordered"
            and isinstance(call.func.value, ast.Name)
            and (call.func.value.id == "StoreLock")
        ):
            return ()
        if self.derives(owner, "StoreLockOwner") and name in {
            "reading",
            "editing",
            "reading_snapshot",
            "read",
            "read_entry",
            "read_conversation",
            "update",
            "replace",
            "mark_historical",
            "adopt",
        }:
            origin = self.method(owner, name)
            origin_site = site
            if origin:
                physical = [
                    n
                    for n in ast.walk(origin[1])
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and (n.func.attr in {"locked", "locked_async", "ordered"})
                ]
                if physical:
                    origin_site = f"{origin[0].path}:{physical[0].lineno}"
                elif name == "read":
                    origin_site = "src/agent_comms/locked_store.py:698"
                else:
                    origin_site = f"{origin[0].path}:{origin[1].lineno}"
            answer = [Acquisition(owner, origin_site, site, ast.unparse(call.func.value))]
            if name == "reading":
                answer += [
                    Acquisition(self.typeof(arg, env), origin_site, site, ast.unparse(arg))
                    for arg in call.args
                    if self.derives(self.typeof(arg, env), "StoreLockOwner")
                ]
            return tuple(answer)
        if budget <= 0:
            return ()
        found = self.method(owner, name)
        if not found:
            return ()
        callee_module, node = found
        if id(node) in trail:
            return ()
        args = node.args.posonlyargs + node.args.args
        offset = 0 if args and args[0].arg not in {"self", "cls"} else 1
        supplied = {
            arg.arg: self.typeof(value, env)
            for arg, value in zip(args[offset:], call.args, strict=False)
        }
        inner_env = self.environment(node, owner, supplied)
        answer = []
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                candidate_envs = [inner_env]
                for loop in ast.walk(node):
                    if (
                        isinstance(loop, ast.For)
                        and isinstance(loop.target, ast.Name)
                        and (inner in tuple(ast.walk(loop)))
                    ):
                        choices = self.iteration_types(loop.iter, inner_env)
                        if choices:
                            candidate_envs = [
                                {**item, loop.target.id: choice}
                                for item in candidate_envs
                                for choice in choices
                            ]
                for candidate_env in candidate_envs:
                    for acquired in self.locks(
                        inner, candidate_env, callee_module, budget - 1, trail + (id(node),)
                    ):
                        answer.append(
                            Acquisition(acquired.store, acquired.site, site, acquired.expression)
                        )
        return tuple(dict.fromkeys(answer))

    def context_locks(self, call, env, module, budget=2):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            return ()
        owner = self.typeof(call.func.value, env)
        name = call.func.attr
        if name in {"locked", "locked_async"} or self.derives(owner, "StoreLockOwner"):
            return self.locks(call, env, module, budget)
        if budget <= 0:
            return ()
        found = self.method(owner, name)
        if not found:
            return ()
        callee_module, node = found
        args = node.args.posonlyargs + node.args.args
        offset = 1 if args and args[0].arg in {"self", "cls"} else 0
        supplied = {
            arg.arg: self.typeof(value, env)
            for arg, value in zip(args[offset:], call.args, strict=False)
        }
        inner_env = self.environment(node, owner, supplied)
        held_at_yield = []

        def find(nodes, active):
            for item in nodes:
                if isinstance(
                    item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
                ):
                    continue
                if isinstance(item, (ast.With, ast.AsyncWith)):
                    held = list(active)
                    for entry in item.items:
                        held.extend(
                            self.context_locks(
                                entry.context_expr, inner_env, callee_module, budget - 1
                            )
                        )
                    find(item.body, held)
                elif isinstance(item, (ast.Yield, ast.YieldFrom)):
                    held_at_yield.extend(active)
                else:
                    find(list(ast.iter_child_nodes(item)), active)

        find(node.body, [])
        return tuple(
            dict.fromkeys(
                Acquisition(lock.store, lock.site, f"{module.path}:{call.lineno}", lock.expression)
                for lock in held_at_yield
            )
        )

    def record(self, outer, inner, active):
        if outer.store in {
            "StoreLockOwner",
            "LockedStore",
            "LockedFile",
            "CarriedDocument",
        } or inner.store in {"StoreLockOwner", "LockedStore", "LockedFile", "CarriedDocument"}:
            return
        borrowed = next((held for held in reversed(active) if held.store == inner.store), None)
        if borrowed is not None:
            self.pairs.add((borrowed, inner, True))
        else:
            self.pairs.add((outer, inner, False))

    def walk(self, nodes, active, env, module):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(node, (ast.With, ast.AsyncWith)):
                held = list(active)
                for item in node.items:
                    acquisitions = self.context_locks(item.context_expr, env, module, 3)
                    if len(acquisitions) > 1:
                        acquisitions = tuple(
                            sorted(acquisitions, key=lambda x: self.ranks.get(x.store, 999))
                        )
                    for acquired in acquisitions:
                        for outer in held:
                            self.record(outer, acquired, held)
                        held.append(acquired)
                self.walk(node.body, held, env, module)
            else:
                if isinstance(node, ast.Call) and active:
                    for inner in self.locks(node, env, module, 2):
                        for outer in active:
                            self.record(outer, inner, active)
                self.walk(list(ast.iter_child_nodes(node)), active, env, module)
