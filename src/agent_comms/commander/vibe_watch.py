"""Print every agent bus message (not notices, not the commander's) so the commander reads and judges each one."""
import json, re, time

from .config import CommanderConfig
from .order import DEADLINES, jargon_in, strike  # one jargon list

CONFIG = CommanderConfig.installed()


def judge(d):
    text, sender = d.get("text") or "", d.get("from")
    if not text or d.get("notice") or d.get("sender_role") != "agent" or sender == CONFIG.commander:
        return
    if words := jargon_in(text):
        # HARD RULE: jargon in a unit message is a strike the moment it is read.
        tasks = sorted({f for o in json.load(open(DEADLINES)).get(sender, []) for f in o.get('facts', [])}) or sorted(set(re.findall(r'\bP\d+\b', text)))[:1]
        print(f"JARGON {sender} [{d.get('id')}]: {', '.join(words)}", flush=True)
        if tasks:
            try:
                strike(sender, f"{tasks[0]} {sender} wrote {', '.join(chr(34) + w + chr(34) for w in words)} in message {d.get('id')} instead of saying in plain words what it did and what broke", f'{CONFIG.commander} at review')
            except SystemExit as refused:
                print(f"JARGON strike refused: {refused}", flush=True)
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
