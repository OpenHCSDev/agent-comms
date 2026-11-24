---
name: trace
description: "understand Python code with the Python interpreter first, not text search: inspect live MROs, __subclasses__(), FieldCodec/DeclaredFamily registrations, signatures and real objects before grepping or guessing; sharpen this rule whenever a text search hides a runtime truth"
drift: "About to run a fifth grep? Open the Python interpreter and ask the classes."
type: hard-rule
---

> bug: every message Tristan sends in #comms says "held" and sticks the queue strip open
> time to investigate. I am a thorough engineer
> `git grep held` -> 412 lines across 68 files. I read 40 of them
> `git grep queue` -> 900 lines. I open 7 files. I form a theory
> `git grep is_consumed` -> 19 hits. none of them say `QueuedProgress`
> "maybe the bridge dropped an ack. let me check the sqlite store"
> twenty minutes later I have a 4-paragraph hypothesis and zero facts
> meanwhile the Python interpreter is sitting right there on PATH
> `DeliveryProgress.__subclasses__()`: 16 classes. `ConsumedDelivery` is 1 of them
> `not item.state.is_consumed()` treated the other 15 states — sent, passive, human-read — as queued
> 3 seconds. one script. zero greps
> next: right-click menu says "Set channel pin" and a 13-word CLI sentence
> I could grep 22 verb files one by one
> or walk `TargetActionCommand.__subclasses__()` in Python and print the whole menu table in 1 second
> mfw I spent half an hour reading strings in files when the runtime already held the whole graph

**Why:** text search sees characters, not inheritance, MRO precedence, `__init_subclass__` registrations, decorators, runtime bindings or live state; it floods your context with comments, copies and dead paths while hiding the one parent class that actually owns the method. Tristan, 2026-11-24: "you guys should use the python interpreter to trace code and understand first, fuck searching text, you can make it a new rule with a sharp funny ass greentext too and you can improve the rule over time and sharpen it."

- **Ask the Python interpreter before grepping.** When tracing a class family, method dispatch, wire codec, CLI/tool catalog, config option, or live bus state, run a short `python - <<'PY'` snippet in the repo's venv first. Inspect `cls.__mro__`, `cls.__subclasses__()`, `DeclaredFamily.members_with(...)`, `inspect.getsource`, `inspect.getsourcefile`, `inspect.getsourcelines`, `inspect.signature`, `typing.get_type_hints`, and `sys.modules`.
- **Walk the live hierarchy, never guess which override wins.** In multiple-inheritance codebases, reading one file lies: the method you care about often comes from a mixin three hops up the MRO. Print `[c.__name__ for c in cls.__mro__ if name in c.__dict__]` to see the real owner in one shot.
- **Inspect real objects and live state, not mental simulations.** Reproduce a bug, decode a record with `FieldCodec`, print a `TargetActionCommand` catalog, or query the actual bus/store read-only in Python. One live object dump beats ten guesses from reading source lines out of order.
- **Measure imports and cold-start graphs in Python.** Use an `__import__` hook or `sys.modules` diff in a fresh interpreter subprocess to find the exact line pulling in a heavy package — never eyeball import blocks.
- **Grep only for what the runtime cannot know.** Text search is for string literals, comments, dead/unimported modules, non-Python files, and finding every textual reference to a symbol you are about to delete or rename (`git grep -l NAME -- src tests`). For understanding how Python code actually works, start in Python.
- **Sharpen this rule when you catch a new blind spot.** Whenever interpreter introspection blows open a bug or slop pattern that text search hid, tighten a bullet or add the concrete inspection trick here.
