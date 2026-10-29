"""The resident commander-session owner judges the model's stop."""

from .watch import SessionHookProgram


class DeferralGuard(SessionHookProgram):
    events = ("Stop",)

    @classmethod
    def command(cls) -> str:
        return cls.console_script(f"{__spec__.name}:main")


main = DeferralGuard.main

if __name__ == "__main__":
    main()
