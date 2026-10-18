"""A CLI command's result owns its exit code and the targets it reconnects."""

from __future__ import annotations


class CliResult:
    """Every result a command returns; a result kind overrides what it decides."""

    __slots__ = ()

    def reconnect_targets(self) -> tuple[str, ...]:
        return ()

    def exit_code(self) -> int:
        return 0
