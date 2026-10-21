"""Delete the carry ledger: each step's own dry run now says whether a bus needs it."""

from agent_comms.installer.carry import RetiredFiles


class RetiredLedger(RetiredFiles):
    @classmethod
    def retired(cls) -> tuple[str, ...]:
        return ("carries.json",)
