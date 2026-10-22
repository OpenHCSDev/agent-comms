"""Print each pytest failure section's header and its last lines, bounded."""
import re
import sys

text = open(sys.argv[1]).read()
limit = int(sys.argv[2])
header = re.compile(r"^_{3,} (.+?) _{3,}$", re.M)
marks = list(header.finditer(text))
seen = set()
for index, mark in enumerate(marks):
    end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
    body = [line[:200] for line in text[mark.end():end].splitlines()
            if line.strip() and not line.startswith(("    ^", "E    +", "-----", "=====")) and "contextlib.py" not in line]
    key = tuple(body[-4:])
    print("##", mark.group(1))
    print("\n".join(body[-limit:]) if key not in seen else "   (same as earlier)")
    seen.add(key)
