"""Delete the retired install schedule; installed build identity now answers what is due."""

from pathlib import Path

from agent_comms.installer.carry import Carried, Carry


class InstallSchedule(Carry):
    def backup_files(self) -> tuple[Path, ...]:
        return (self.root / "install-deadline.json",)

    def carry(self) -> Carried:
        paths = tuple(path for path in self.backup_files() if path.exists())
        for path in paths:
            self.require_confined(self.root, path)
            if self.apply:
                path.unlink()
        return Carried(len(paths), {}, tuple(path.name for path in paths) if self.apply else ())
