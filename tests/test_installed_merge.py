"""Merge observation closes the real stored order, without a completion request."""
from agent_comms.repository_name import RepositoryName
from dataclasses import fields, replace
import json
import subprocess

import pytest

from agent_comms.repo import Repo
from agent_comms.field_codec import FieldCodec
from agent_comms.plan_phases import RecordPlanePhase
from agent_comms.todos import (CheckedProblemOrder, DoneTodoState, MergedProblemOrder, OperationDelivery,
                               MergedPullRequest, PullRequests, UnorderedProblemOrder)
from agent_comms.tools import CommsOrderDoneTool, CommsOrderServeTool
from agent_comms.recorded_time import RecordedEventTime
from cutover.carry_merged_orders import MergedOrders
from test_commander_problem_orders import working
from test_order_progress import checkout, commit, order_alarms, git


def test_failed_git_is_not_a_negative_ancestry_result(tmp_path):
    with pytest.raises(subprocess.CalledProcessError):
        Repo(checkout=tmp_path / 'missing', github=RepositoryName.from_wire('test/repo')).contains('a' * 40)


def test_a_merge_made_after_the_last_fetch_is_fetched_then_answered(tmp_path):
    origin, installed = tmp_path / 'origin', tmp_path / 'installed'
    origin.mkdir()
    git(origin, 'init', '-q', '-b', 'main')
    git(origin, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'base')
    git(tmp_path, 'clone', '-q', str(origin), str(installed))
    git(origin, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'merged later')
    sha = git(origin, 'rev-parse', 'HEAD')
    repo = Repo(checkout=installed, github=RepositoryName.from_wire('test/repo'))

    assert not repo.contains(sha)
    assert repo.contains(sha, 'origin/main')
    assert not repo.contains('b' * 40)
