"""Measured physical room shared by thread presence and work admission."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoomNeed:
    memory: int = 0
    disk: int = 0

    def __add__(self, other: RoomNeed) -> RoomNeed:
        return RoomNeed(self.memory + other.memory, self.disk + other.disk)

    def scaled(self, count: int) -> RoomNeed:
        return RoomNeed(self.memory * count, self.disk * count)

    def divided(self, count: int) -> RoomNeed:
        return RoomNeed((self.memory + count - 1) // count, (self.disk + count - 1) // count)

    def remaining(self, other):
        return RoomNeed(max(0, self.memory - other.memory), max(0, self.disk - other.disk))

    def fits(self, free):
        return self.memory <= free.memory and self.disk <= free.disk

    def peak(self, other):
        return RoomNeed(max(self.memory, other.memory), max(self.disk, other.disk))
