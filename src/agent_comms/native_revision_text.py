"""The SDK EntryStore's external revision scalar, represented by FileRevision."""
import re

from .field_codec import TextRepresentation
from .private_path import FileIdentity, FileRevision


class NativeRevisionText(TextRepresentation):
    @classmethod
    def encode(cls, value):
        if not isinstance(value, FileRevision):
            raise TypeError("Native revision requires the original FileRevision")
        return ":".join(map(str, (value.identity.device, value.identity.inode,
                                 value.size, value.mtime_ns, value.ctime_ns)))

    @classmethod
    def from_text(cls, value):
        if re.fullmatch(r"[0-9]+:[0-9]+:[0-9]+:[0-9]+:[0-9]+", value) is None:
            raise ValueError("Invalid native SDK revision")
        device, inode, size, mtime, ctime = map(int, value.split(":"))
        revision = FileRevision(FileIdentity(device, inode), size, mtime, ctime)
        if cls.encode(revision) != value:
            raise ValueError("Noncanonical native SDK revision")
        return revision
