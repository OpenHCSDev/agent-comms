"""Recognize pytest at the shell boundary; order checks never launch it."""

from .shell_commands import Program, ShellCommand


class PythonTestProgram(Program):
    def runs(self, command: ShellCommand) -> bool:
        if super().runs(command):
            return True
        args = command.args
        return (command.program.startswith("python") and "-m" in args[:-1]
                and args[args.index("-m") + 1] == self.name)

    def require_integrator(self, command: str) -> None:
        if any(self.runs(simple) for simple in ShellCommand.split(command)):
            raise ValueError(
                "Order checks never launch pytest; give the integrator the test paths "
                "and the unresolved risk that requires the run."
            )


PYTEST = PythonTestProgram("pytest")
