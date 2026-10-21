"""Delete the retired install schedule; installed build identity now answers what is due."""

from agent_comms.installer.carry import RetiredFiles


class InstallSchedule(RetiredFiles):
    @classmethod
    def retired(cls) -> tuple[str, ...]:
        return ("install-deadline.json",)
