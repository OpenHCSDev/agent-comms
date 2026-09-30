# Borrowed test dependency correction

The initial environment audit protected default installation `.pth` files but omitted borrowed dependency `.pth` files inside retained worktrees. This removed TC2's generated `.venv` while open Toad221 still borrowed its site-packages. This was a cleanup error, not an application defect. Einstein437 also reported an initial import failure before fixture creation, and switched its dependency-only path to a protected installed runtime. No provider attempt or original input was replayed.

Parent restored TC2's original persistent environment from its unchanged committed `uv.lock`, using `uv sync --frozen --no-install-project`, then restored Textual's declared `pytest-asyncio` extra. The actual221 interpreter successfully imports its own Core/Toad first, original Textual412 and SDK0.12.1, Pillow, pytest, pyte, Xlib and psutil. Kepler confirmed the original bootstrap/pins and resumes the same source proof command. The cleanup operator now scans retained-worktree `.pth` references too; mutation was not rerun.

`borrowed-dependency-audit.json` preserves the actual missed references. The only missed current open-PR dependency is now restored. One historical unused runtime (`comms-compaction-live-fix/.artifacts/runtime`) retains a dangling pointer to an old generated environment; its original source and evidence remain, and it is not used by any active gate. It must be rebuilt from its retained lock before historical reuse.

All removed environment paths remain in the original receipt; do not erase the failed audit result or count restored TC2 space as recovered. No default installed package, original native session, live bus or uncertain input was changed.
