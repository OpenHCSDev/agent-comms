"""The source-line measure shared by branch growth and its readers."""

import ast
import io
import tokenize


class CodeLines:
    @classmethod
    def of(cls, source: str) -> int:
        module = ast.parse(source)
        leading: list[ast.stmt] = []
        statements = module.body
        if ast.get_docstring(module, clean=False) is not None:
            leading.append(statements[0])
            statements = statements[1:]
        for statement in statements:
            if not isinstance(statement, (ast.Import, ast.ImportFrom)):
                break
            leading.append(statement)
        excluded = {
            line
            for statement in leading
            for line in range(statement.lineno, statement.end_lineno + 1)
        }
        # A remaining statement can share an import/docstring's physical line.
        excluded.difference_update(
            statement.lineno for statement in module.body if statement not in leading
        )
        comments = {
            token.start[0]
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT and not token.line[: token.start[1]].strip()
        }
        return sum(
            1
            for number, line in enumerate(source.splitlines(), 1)
            if line.strip() and number not in excluded and number not in comments
        )
