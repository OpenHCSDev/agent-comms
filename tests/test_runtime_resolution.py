import pytest

from cutover.runtime_resolution import ResolvedLock, RuntimeResolution


def manifest(core="core", toad="toad", wheel="old-wheel"):
    return RuntimeResolution.model_validate(
        {
            "core_commit": core,
            "toad_commit": toad,
            "packages": {
                "agent-comms": {"version": "1", "source": {"url": wheel, "archive_info": {}}},
                "batrachian-toad": {"version": "2", "source": {"url": wheel, "archive_info": {}}},
                "textual": {
                    "version": "3",
                    "source": {
                        "url": "https://example/textual",
                        "vcs_info": {"commit_id": "textual"},
                    },
                },
                "rich": {"version": "4", "source": None},
            },
        }
    )


def lock():
    return ResolvedLock.model_validate(
        {
            "package": [
                {"name": "agent-comms", "version": "1", "source": {"editable": ".."}},
                {
                    "name": "batrachian-toad",
                    "version": "2",
                    "source": {"git": "https://example/toad#toad"},
                },
                {
                    "name": "textual",
                    "version": "3",
                    "source": {"git": "https://example/textual#textual"},
                },
                {"name": "rich", "version": "4", "source": {"registry": "https://pypi.org/simple"}},
                # The universal lock contains entries not selected by this synced environment.
                {
                    "name": "unselected",
                    "version": "5",
                    "source": {"registry": "https://pypi.org/simple"},
                },
            ]
        }
    )


def test_recorded_sources_match_selected_lock_not_wheel_locations():
    built = manifest()
    selected = {name: record.version for name, record in built.packages.items()}
    pins = lock().pins(selected, "core")
    assert built.pins() == pins
    assert manifest(wheel="another-wheel").pins() == pins
    assert manifest(core="changed").pins() != pins
    assert manifest(toad="changed").pins() != pins
    assert lock().pins({**selected, "unselected": "5"}, "core") != built.pins()


def test_missing_and_ambiguous_lock_evidence_refuses():
    with pytest.raises(ValueError, match="0 entries"):
        lock().pins({"missing": "1"}, "core")
    ambiguous = lock()
    ambiguous.package.append(ambiguous.package[0])
    with pytest.raises(ValueError, match="2 entries"):
        ambiguous.pins({"agent-comms": "1"}, "core")
