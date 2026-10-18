"""Print every agent bus message (not notices, not the commander's) so the commander reads and judges each one."""
import json, time

from .config import CommanderConfig
from ..todos import PlainText
from .order import post

CONFIG = CommanderConfig.installed()


def judge(d):
    text, sender = d.get("text") or "", d.get("from")
    if not text or d.get("notice") or d.get("sender_role") != "agent" or sender == CONFIG.commander:
        return
    if words := PlainText.found(text):
        print(f"JARGON {sender} [{d.get('id')}]: {', '.join(words)}", flush=True)
        post([sender], f"Correction, no strike: message {d.get('id')} used {', '.join(words)}. "
             "Say what you did and what broke in plain words from now on.")
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
