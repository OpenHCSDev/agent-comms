"""One authorized C3 member retirement, outside every production reader.

Original installed declarations validate input first. Target declarations then
validate the complete projected value. This operation owns both durable Thread
carriers and the bounded private-fixture observation of the same member.
"""
from typing import ClassVar


class GoalReportMemberRetirement:
    member: ClassVar[str] = 'last_goal_report_turn'

    @classmethod
    def thread(cls, original: dict) -> dict:
        target = dict(original)
        # An already-target record is not an invitation to repeat a cutover.
        del target[cls.member]
        return target

    @classmethod
    def threads(cls, original: dict) -> dict:
        target = dict(original)
        target['threads'] = {
            name: cls.thread(thread) for name, thread in original['threads'].items()
        }
        return target

    @classmethod
    def releases(cls, original: dict) -> dict:
        return {
            name: dict(receipt, thread=cls.thread(receipt['thread']))
            for name, receipt in original.items()
        }
