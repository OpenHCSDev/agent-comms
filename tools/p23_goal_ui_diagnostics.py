"""Launch-time diagnostics for P23's private native UI, never live-process injection."""

import asyncio
from contextlib import contextmanager
from types import CoroutineType, GeneratorType

from textual import events
from textual._context import message_hook
from textual.errors import NoWidget
from toad.core.input_events import GoalControlActivated
from toad.core_event_carrier import CoreEventMessage


def record_goal_controls(native, stage: str) -> None:
    view = native.ui.conversation
    print(f"P23 GOAL {stage}: display={type(view.goal_display).__qualname__} control={view.goal_display.can_control} owns={view.goal_controls.owns(view.agent_binding)}", flush=True)
    for control in view.goal_bar.actions:
        try:
            hit, _ = view.screen.get_widget_at(control.region.x + control.region.width // 2,
                                               control.region.y + control.region.height // 2)
        except NoWidget:
            hit = None
        print(f"P23 CONTROL {stage} {control.interaction.__qualname__}: disabled={control.disabled} region={control.region} hit={hit!r}", flush=True)


def record_input(message) -> None:
    match message:
        case events.MouseDown() | events.MouseUp() | events.Click():
            print(f"P23 INPUT {type(message).__qualname__}: widget={message.widget!r} screen=({message.screen_x},{message.screen_y})", flush=True)
        case CoreEventMessage(event=GoalControlActivated()):
            print(f"P23 ACTIVATION {message.event.action.__qualname__}: publisher={message.publisher!r}", flush=True)


@contextmanager
def goal_input_trace():
    token = message_hook.set(record_input)
    try:
        yield
    finally:
        message_hook.reset(token)


def dump_async_waits() -> None:
    for task in asyncio.all_tasks():
        chain = []
        current = task.get_coro()
        while current is not None:
            match current:
                case CoroutineType():
                    frame = current.cr_frame
                    current = current.cr_await
                case GeneratorType():
                    frame = current.gi_frame
                    current = current.gi_yieldfrom
                case _:
                    break
            if frame is not None:
                chain.append(f"{frame.f_code.co_filename}:{frame.f_lineno}:{frame.f_code.co_name}")
        print(f"P23 ASYNC {task.get_name()}: {' -> '.join(chain)}", flush=True)
