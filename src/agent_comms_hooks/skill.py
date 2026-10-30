"""The resident session owner deals its existing rule sources and reminder deck."""

from .watch import SessionHookProgram


class SkillText(SessionHookProgram):
    events = ("SessionStart",)

    @classmethod
    def installation_members(cls) -> tuple[type[SessionHookProgram], ...]:
        """The startup owner deals after launch; do not install a parallel racing hook."""
        return ()


main = SkillText.main

if __name__ == "__main__":
    main()
