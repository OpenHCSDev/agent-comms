"""A wire-lock body must never suspend its event loop while holding custody."""

import ast
from pathlib import Path


SOURCE_ROOT = Path("src/agent_comms")


def _called_name(expression: ast.expr) -> str:
    if not isinstance(expression, ast.Call):
        return ""
    function = expression.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return ""


def _wire_scopes(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        if any(_called_name(item.context_expr) == "_async_store_lock"
               for item in node.items):
            yield node


def _await_lines(statements: list[ast.stmt]) -> tuple[int, ...]:
    return tuple(
        child.lineno
        for statement in statements
        for child in ast.walk(statement)
        if isinstance(child, ast.Await)
    )


def _violations(sources: dict[str, str]):
    trees = {
        filename: ast.parse(source, filename=filename)
        for filename, source in sources.items()
    }
    functions = tuple(
        function
        for tree in trees.values()
        for function in ast.walk(tree)
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    yielding_lock_managers = {
        function.name
        for function in functions
        if any(any(isinstance(child, (ast.Yield, ast.YieldFrom))
                    for child in ast.walk(scope))
               for scope in _wire_scopes(function))
    }
    while True:
        inherited = {
            function.name
            for function in functions
            if any(
                isinstance(scope, ast.AsyncWith)
                and any(_called_name(item.context_expr) in yielding_lock_managers
                        for item in scope.items)
                and any(isinstance(child, (ast.Yield, ast.YieldFrom))
                        for child in ast.walk(scope))
                for scope in ast.walk(function)
            )
        }
        if inherited <= yielding_lock_managers:
            break
        yielding_lock_managers.update(inherited)
    violations = []
    for filename, tree in trees.items():
        for scope in _wire_scopes(tree):
            if awaits := _await_lines(scope.body):
                violations.append((filename, scope.lineno, awaits))
        for scope in ast.walk(tree):
            if not isinstance(scope, ast.AsyncWith):
                continue
            if not any(_called_name(item.context_expr) in yielding_lock_managers
                       for item in scope.items):
                continue
            if awaits := _await_lines(scope.body):
                violations.append((filename, scope.lineno, awaits))
    return violations


def test_no_wire_lock_scope_or_yielded_caller_contains_await():
    sources = {
        str(path): path.read_text()
        for path in SOURCE_ROOT.rglob("*.py")
    }
    assert sources
    assert not _violations(sources)


def test_yielded_lock_managers_are_derived_from_declarations():
    sources = {
        "manager.py": """
async def borrowed():
    async with _async_store_lock(path):
        yield
""",
        "wrapper.py": """
async def relayed():
    async with borrowed():
        yield
""",
        "caller.py": """
async def use():
    async with relayed():
        await suspended()
""",
    }
    assert _violations(sources) == [("caller.py", 3, (4,))]
