"""Publication rules use a real Git repository, shared frame, stores and forge subprocess."""

import asyncio
import time
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime

from agent_comms.commander import judge
from agent_comms.commander_heartbeat import CommanderHeartbeat
from agent_comms.closed_pull_requests import ClosedPullRequestWatch
from agent_comms.commander.publication_rules import (
    ClosedPullRequestScanPending, ClosedUnmergedPullRequest,
)
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.head_review import PullRequestHead
from commander_heartbeat_fixture import capture_frame
from agent_comms.heartbeat_subjects import CommanderHistory
from agent_comms.order_tools import CommsHeadVerdictTool
from agent_comms.recorded_time import RecordedEventTime
from agent_comms.task_attachments.commander_pings import RulePing
from agent_comms.tool_caller import ThreadCaller
from agent_comms.work_branches import SharedCommitBase, UnrelatedCommitBase
from agent_comms.todos import (
    ClosedListedPullRequest, MergedListedPullRequest, NoHeadOrder,
    ProgressRead, PullRequestCommit, ReviewedHeadVerdict, SupersededHeadVerdict, VerdictReason,
)

pytest_plugins = ('test_head_review',)


def test_unscanned_repository_is_pending_not_lost(head_bus, reviewed_source):
    comms, github, _ = head_bus
    source = reviewed_source
    stamp = datetime.fromtimestamp(time.time() - 601, UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    github.put(source.github, ClosedListedPullRequest(
        7, 'closed/7', source.branch_commit, closed_at=stamp, updated=stamp,
        base='main', reviewable_times=(stamp,)))
    repos = comms.registry.snapshot().commander_project.require().repository_names()
    plans = CommanderHeartbeat()

    def rules(frame):
        return (ClosedUnmergedPullRequest.scan_observed(comms, 'lead', (), frame),
                ClosedPullRequestScanPending.scan_observed(comms, 'lead', (), frame))

    lost, pending = rules(capture_frame(comms, 'lead', ProgressRead(()), plans))
    assert lost == ()
    assert {rule.repo for rule in pending} == set(repos)

    watch = ClosedPullRequestWatch()
    asyncio.run(watch.run(lambda: nullcontext(comms)))
    lost, pending = rules(capture_frame(comms, 'lead', ProgressRead(()), plans))
    assert {rule.request.pr for rule in lost} == {7}
    assert source.github not in {rule.repo for rule in pending}


def test_unrelated_closed_head_does_not_poison_its_repository(head_bus, reviewed_source):
    comms, github, _ = head_bus
    source = reviewed_source
    stamp = datetime.fromtimestamp(time.time() - 601, UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    tree = source.git(source.checkout, 'rev-parse', 'main^{tree}').strip()
    unrelated = source.git(source.checkout, 'commit-tree', tree,
                           '-m', 'Independent history, no parent').strip()
    for number, head in ((7, source.branch_commit), (10, unrelated)):
        github.put(source.github, ClosedListedPullRequest(
            number, f'closed/{number}', head, updated=stamp, closed_at=stamp,
            base='main', reviewable_times=(stamp,)))
    watch = ClosedPullRequestWatch()
    asyncio.run(watch.run(lambda: nullcontext(comms)))
    frame = capture_frame(comms, 'lead', ProgressRead(()), CommanderHeartbeat())
    rules = {rule.request.pr: rule for rule in
             ClosedUnmergedPullRequest.scan_observed(comms, 'lead', (), frame)}
    assert set(rules) == {7, 10}
    assert rules[7].base == SharedCommitBase(source.main_commit)
    assert rules[10].base == UnrelatedCommitBase()
    assert frame.closed.unscanned == ()
    assert FieldCodec.decode(RulePing, FieldCodec.encode(RulePing.capture(rules[10]))) == RulePing.capture(rules[10])


def test_closed_unmerged_pull_request_flags_lost_commits(head_bus, reviewed_source, installed_runtime):
    comms, github, _ = head_bus
    source = reviewed_source
    stamp = datetime.fromtimestamp(time.time() - 601, UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    original_main = source.main_commit
    lost_head = source.branch_commit

    # A multi-commit squash defeats both SHA ancestry and individual git-cherry patch IDs.
    (source.checkout / 'squashed.py').write_text('landed = True\n')
    source.git(source.checkout, 'add', 'squashed.py')
    source.git(source.checkout, 'commit', '-m', 'Original squashed work')
    (source.checkout / 'second.py').write_text('also_landed = True\n')
    source.git(source.checkout, 'add', 'second.py')
    source.git(source.checkout, 'commit', '-m', 'Second squashed commit')
    squashed_head = source.main_commit
    source.git(source.checkout, 'reset', '--soft', original_main)
    source.git(source.checkout, 'commit', '-m', 'Squash landing with different identity')
    landed = source.main_commit
    assert squashed_head != landed
    source.git(source.checkout, 'push', 'origin', 'main')

    common = dict(updated=stamp, base='main', reviewable_times=(stamp,))
    old = datetime.fromtimestamp(time.time() - 30 * 24 * 60 * 60,
                                UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    github.put(source.github, ClosedListedPullRequest(
        12, 'old-unresolved', lost_head, closed_at=old, updated=old,
        base='main', reviewable_times=(old,)))
    for number, head in ((7, lost_head), (8, squashed_head)):
        github.put(source.github, ClosedListedPullRequest(
            number, f'closed/{number}', head, closed_at=stamp, **common))
    github.put(source.github, MergedListedPullRequest(
        9, 'merged', landed, merge=PullRequestCommit(landed), merged=stamp, **common))

    def scan(frame):
        return ClosedUnmergedPullRequest.scan_observed(comms, 'lead', (), frame)

    watch = ClosedPullRequestWatch()
    plans = CommanderHeartbeat()

    def capture():
        asyncio.run(watch.run(lambda: nullcontext(comms)))
        return capture_frame(comms, 'lead', ProgressRead(()), plans)

    frame = capture()
    found = {rule.request.pr: rule for rule in scan(frame)}
    assert set(found) == {7, 12}
    lost, old_lost = found[7], found[12]
    request = PullRequestHead(source.github, 7, lost_head)
    assert lost.request == request
    assert lost.commits == (lost_head,)
    assert lost.line() == (f'LOST https://github.com/{source.github.to_wire()}/pull/7 '
        'closed unmerged holds 1 commits main lacks -> land them, or record why they are dropped')
    assert judge.SupersededVerb in ClosedUnmergedPullRequest.clearing_verbs
    accepted = ReviewedHeadVerdict(VerdictReason('The owner implements the requested behavior without competing decisions.'))
    review = CommsHeadVerdictTool(request=request, operation=accepted, order=NoHeadOrder())
    review.invoke(comms, review.arguments, caller=ThreadCaller('lead'))
    assert set(scan(capture())) == {lost, old_lost}
    ping = replace(RulePing.capture(lost), since=RecordedEventTime(lost.began()))
    assert not ping.acted(CommanderHistory.read(comms, frozenset(('lead',))))
    assert FieldCodec.decode(RulePing, FieldCodec.encode(ping)) == ping

    verdict = judge.SupersededVerb(
        pr=request.pr, head=request.head, repo=request.repo.to_wire(), by='P2',
        why='The replacement includes the useful work and deliberately drops this obsolete implementation.').head_operation()
    assert verdict == SupersededHeadVerdict(VerdictReason(
        'The replacement includes the useful work and deliberately drops this obsolete implementation.'), 'P2')
    tool = CommsHeadVerdictTool(request=request, operation=verdict, order=NoHeadOrder())
    tool.invoke(comms, tool.arguments, caller=ThreadCaller('lead'))
    assert set(scan(frame)) == {lost, old_lost}  # A later write cannot alter this snapshot.
    assert scan(capture()) == (old_lost,)

    # Restore an ordinary closed head, then prove Git alone clears it after landing.
    github.put(source.github, ClosedListedPullRequest(
        11, 'closed/to-land', lost_head, closed_at=stamp, **common))
    before = scan(capture())
    assert {rule.request.pr for rule in before} == {11, 12}
    source.git(source.checkout, 'merge', '--no-edit', 'feature')
    source.git(source.checkout, 'push', 'origin', 'main')
    assert scan(capture()) == ()
