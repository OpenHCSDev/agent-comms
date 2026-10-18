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


def _calls(node: ast.AST, names: set[str]) -> tuple[int, ...]:
    return tuple(
        call.lineno
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and _called_name(call) in names
    )


def _yields(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in ast.walk(function))


def _acquires_flock(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for call in ast.walk(function):
        if not isinstance(call, ast.Call) or _called_name(call) != "flock":
            continue
        flags = (node for argument in call.args[1:] for node in ast.walk(argument))
        if not any(isinstance(node, ast.Attribute) and node.attr == "LOCK_UN" for node in flags):
            return True
    return False


def _starts_write_transaction(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for call in ast.walk(function):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "execute"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value in {"BEGIN IMMEDIATE", "BEGIN EXCLUSIVE"}
        ):
            return True
    return False


def _borrowed_contexts(function: ast.FunctionDef | ast.AsyncFunctionDef):
    for node in ast.walk(function):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            yield from (item.context_expr for item in node.items)
        elif isinstance(node, ast.Call) and _called_name(node) == "enter_context" and node.args:
            yield node.args[0]


def _yielding_wrappers(
    functions: tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...], managers: set[str]
) -> set[str]:
    return {
        function.name
        for function in functions
        if _yields(function)
        and any(_calls(context, managers) for context in _borrowed_contexts(function))
    }


def _derived_managers(
    functions: tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...], direct: set[str]
) -> set[str]:
    managers = set(direct)
    while True:
        inherited = _yielding_wrappers(functions, managers)
        if inherited <= managers:
            return managers
        managers.update(inherited)


def _file_lock_managers(
    functions: tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...],
) -> set[str]:
    physical = {function.name for function in functions if _acquires_flock(function)}
    direct = {
        function.name
        for function in functions
        if _yields(function)
        and (_acquires_flock(function) or any(_calls(function, physical)))
    }
    return _derived_managers(functions, direct)


def _write_transaction_managers(
    functions: tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...],
) -> set[str]:
    direct = {
        function.name
        for function in functions
        if _yields(function) and _starts_write_transaction(function)
    }
    return _derived_managers(functions, direct)


def _exit_stack_name(item: ast.withitem) -> str | None:
    if _called_name(item.context_expr) != "ExitStack" or not isinstance(item.optional_vars, ast.Name):
        return None
    return item.optional_vars.id


def _entered_context(call: ast.Call, stack_name: str) -> ast.expr | None:
    function = call.func
    if (
        not isinstance(function, ast.Attribute)
        or function.attr != "enter_context"
        or not isinstance(function.value, ast.Name)
        or function.value.id != stack_name
        or not call.args
    ):
        return None
    return call.args[0]


def _sql_file_lock_violations(sources: dict[str, str]):
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
    file_locks = _file_lock_managers(functions)
    transactions = _write_transaction_managers(functions)
    violations: dict[tuple[str, int], set[int]] = {}

    def record(filename: str, transaction_line: int, lock_lines: tuple[int, ...]) -> None:
        if lock_lines:
            violations.setdefault((filename, transaction_line), set()).update(lock_lines)

    for filename, tree in trees.items():
        for scope in ast.walk(tree):
            if not isinstance(scope, (ast.With, ast.AsyncWith)):
                continue
            for index, item in enumerate(scope.items):
                transaction_lines = _calls(item.context_expr, transactions)
                if not transaction_lines:
                    continue
                later = tuple(other.context_expr for other in scope.items[index + 1:]) + tuple(scope.body)
                lock_lines = tuple(
                    line for node in later for line in _calls(node, file_locks)
                )
                for transaction_line in transaction_lines:
                    record(filename, transaction_line, lock_lines)

            for item in scope.items:
                stack_name = _exit_stack_name(item)
                if stack_name is None:
                    continue
                active_transactions: list[int] = []
                calls = sorted(
                    (call for statement in scope.body for call in ast.walk(statement)
                     if isinstance(call, ast.Call)),
                    key=lambda call: (call.lineno, call.col_offset),
                )
                for call in calls:
                    entered = _entered_context(call, stack_name)
                    if entered is None:
                        continue
                    lock_lines = _calls(entered, file_locks)
                    for transaction_line in active_transactions:
                        record(filename, transaction_line, lock_lines)
                    active_transactions.extend(_calls(entered, transactions))

    return [
        (filename, transaction_line, tuple(sorted(lock_lines)))
        for (filename, transaction_line), lock_lines in sorted(violations.items())
    ]


def test_no_file_lock_is_acquired_inside_a_write_transaction():
    sources = {
        str(path): path.read_text()
        for path in SOURCE_ROOT.rglob("*.py")
    }
    assert sources
    assert not _sql_file_lock_violations(sources)


def test_sql_file_lock_gate_derives_context_managers_from_declarations():
    sources = {
        "manager.py": """def physical_lock():
    fcntl.flock(fd, fcntl.LOCK_EX)
    yield

def relayed_lock():
    with physical_lock():
        yield
""",
        "transaction.py": """def transaction():
    db.execute(\"BEGIN IMMEDIATE\")
    yield db
""",
        "nested.py": """def use():
    with transaction():
        with relayed_lock():
            pass
""",
        "exit_stack.py": """def use():
    with ExitStack() as custody:
        custody.enter_context(transaction())
        custody.enter_context(relayed_lock())
""",
    }
    assert _sql_file_lock_violations(sources) == [
        ("exit_stack.py", 3, (4,)),
        ("nested.py", 2, (3,)),
    ]
