"""Print every agent bus message (not notices, not the commander's) so the commander reads and judges each one."""
import json, time

from .config import CommanderConfig
from ..tools import CommsCorrectWordingTool
from .order import call

CONFIG = CommanderConfig.installed()


def judge(d):
    text, sender = d.get("text") or "", d.get("from")
    if not text or d.get("notice") or d.get("sender_role") != "agent" or sender == CONFIG.commander:
        return
    try:  # Core decides whether the wording needs a correction and records it on the unit's orders
        call(CommsCorrectWordingTool(unit=sender, message_id=d.get("id"), text=text))
        print(f"CORRECTED {sender} [{d.get('id')}]", flush=True)
    except ValueError:
        pass
    print(f"MSG {sender} -> {d.get('to')} [{d.get('id')}]: {' '.join(text.split())[:1200]}", flush=True)


def main(a):
    """Follow the bus from its current end; only messages written from now on are judged."""
    with open(CONFIG.root / 'bus.jsonl', 'rb') as bus:
        bus.seek(0, 2)
        pending = b''
        while True:
            pending += bus.read()
            *lines, pending = pending.split(b'\n')
            for line in lines:
                try:
                    judge(json.loads(line))
                except ValueError:
                    continue
            if not lines:
                time.sleep(1)
