"""SessionEnd uses the existing resident owner without importing Core."""
from .watch import SessionHookProgram


class SessionEnded(SessionHookProgram):
    events = ("SessionEnd",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{ended.__module__}:{ended.__name__}")


def ended() -> None:
    SessionEnded.main()
