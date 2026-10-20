"""The append owner schedules optional work after the whole lock lifetime."""

from contextvars import Context


from agent_comms.store_files import StoreLock



def test_release_scope_closes_after_context_switch_and_stale_context_starts_fresh():
    calls = []
    entered, exited = Context(), Context()
    release = StoreLock.releasing()

    entered.run(release.__enter__)
    entered.run(lambda: StoreLock.current_release().callback(calls.append, "first"))
    exited.run(release.__exit__, None, None, None)
    assert calls == ["first"]

    def independent():
        with StoreLock.releasing():
            StoreLock.current_release().callback(calls.append, "second")
            assert calls == ["first"]

    entered.run(independent)
    assert calls == ["first", "second"]


def test_nested_release_scope_runs_only_after_outer_release():
    calls = []
    with StoreLock.releasing() as outer:
        with StoreLock.releasing() as inner:
            assert inner is outer
            StoreLock.current_release().callback(calls.append, "released")
        assert calls == []
    assert calls == ["released"]


