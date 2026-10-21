"""Delete the retired install schedule; installed build identity now answers what is due."""

from pathlib import Path

from agent_comms.installer.carry import Carried, Carry


class InstallSchedule(Carry):
    @classmethod
    def backup_files(cls, root: Path) -> tuple[Path, ...]:
        return (root / "install-deadline.json",)

    @classmethod
    def carry(cls, root: Path, *, apply: bool) -> Carried:
        paths = tuple(path for path in cls.backup_files(root) if path.exists())
        for path in paths:
            cls.require_confined(root, path)
            if apply:
                path.unlink()
        return Carried(len(paths), {}, tuple(path.name for path in paths) if apply else ())
