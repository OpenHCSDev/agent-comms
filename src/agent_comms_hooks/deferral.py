"""The resident session owner judges the model's stop."""

from .watch import SessionHookProgram


class DeferralGuard(SessionHookProgram):
    events = ("Stop",)


if __name__ == "__main__":
    DeferralGuard.main()
