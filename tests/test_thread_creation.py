"""Creation contracts through the actual CLI decoder, editor binding and registry.

Author does not execute these controls; the integration owner selects them once.
"""
from dataclasses import replace

import pytest

from agent_comms.cli import build_parser
from agent_comms.cli_command import PendingCommandBinding
from agent_comms.cli_verbs.fork import ForkCliCommand
from agent_comms.cli_verbs.new import NewThreadCliCommand
from agent_comms.field_codec import MalformedRecord
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.repo import Checkout
from agent_comms.thread_management import GivenTags, InheritedTags


def parsed(arguments):
    namespace = build_parser(arguments).parse_args(arguments)
    return namespace.command_declaration.from_arguments(namespace)


def test_new_cli_requires_an_explicit_tag_argument(source_repository):
    with pytest.raises(MalformedRecord):
        parsed(['new', '--name', 'child', '--project', str(source_repository.checkout)])


@pytest.mark.parametrize('text,expected', (
    ('auth,review', frozenset({'auth', 'review'})),
    ('', frozenset()),
))
def test_new_cli_membership_reaches_registration(comms, source_repository, text, expected):
    command = parsed(['new', '--name', 'child', '--project', str(source_repository.checkout),
                      '--tags', text])
    assert command.tags == GivenTags(expected)
    # New's declaration path reserves its session but does not launch a provider.
    child = command.specification().declare(comms.threads, '', LaunchRuntime.running())
    assert child.tags == expected
    assert comms.registry.require(child.name).tags == expected


def test_pending_new_binding_offers_missing_tags_without_constructing_the_command(comms, source_repository):
    binding, = NewThreadCliCommand.project_bindings(comms, Checkout(source_repository.checkout))
    assert type(binding) is PendingCommandBinding
    tags = next(item for item in binding.editable_fields if item.name == 'tags')
    assert tags.required and not tags.supplied
    assert tags.editor_default is None
    with pytest.raises(MalformedRecord):
        binding.edited(NewThreadCliCommand.editor_arguments({'name': 'child'}))


@pytest.mark.parametrize('text,expected', (
    ('auth', frozenset({'auth'})),
    ('', frozenset()),
))
def test_pending_new_binding_distinguishes_explicit_empty_from_missing(comms, source_repository, text, expected):
    binding, = NewThreadCliCommand.project_bindings(comms, Checkout(source_repository.checkout))
    command = binding.edited(NewThreadCliCommand.editor_arguments({'name': 'child', 'tags': text}))
    assert command.tags == GivenTags(expected)
    assert command.project == Checkout(source_repository.checkout)


def test_pending_catalog_context_can_change_before_tags_are_supplied(comms, source_repository):
    binding, = NewThreadCliCommand.project_bindings(comms, Checkout(source_repository.checkout))
    prepared = binding.for_fields({'name': 'child'})
    assert prepared.context.name == 'child'
    assert not next(item for item in prepared.editable_fields if item.name == 'tags').supplied


def test_fork_cli_defaults_to_actual_parent_membership(comms, source_repository):
    parent_command = parsed(['new', '--name', 'parent', '--project', str(source_repository.checkout),
                             '--tags', 'original'])
    parent = parent_command.specification().declare(comms.threads, '', LaunchRuntime.running())
    command = parsed(['fork', '--name', 'child', '--parent', parent.name,
                      '--project', str(source_repository.checkout)])
    assert command.tags == InheritedTags()
    editor = ForkCliCommand.thread_binding(parent)
    assert editor.tags == InheritedTags()
    # A real later parent record, not the stale tags at editor opening, supplies membership.
    current = comms.registry.declare(replace(parent, tags=frozenset({'current'})))
    child = command.specification().child(current, current.runtime, comms.threads)
    assert child.tags == current.tags == frozenset({'current'})
    assert editor.tags.for_child(current.tags) == current.tags
