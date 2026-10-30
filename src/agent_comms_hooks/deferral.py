"""The resident commander-session owner judges the model's stop."""

from .watch import SessionHookProgram


class DeferralGuard(SessionHookProgram):
    events = ("Stop",)


main = DeferralGuard.main

if __name__ == "__main__":
    main()
