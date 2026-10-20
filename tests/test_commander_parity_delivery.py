from agent_comms.commander_parity.delivery import DeliveryRepository, DeliveredCommitCase
from agent_comms.commander_parity.rows import DeliveredCommitParityRow
from agent_comms.goals import CountCheckResult, OrderCheck


def test_delivery_graph_is_pushed_and_distinguishes_integration(tmp_path):
    repository = DeliveryRepository.create(tmp_path)
    main, branch, unmerged = repository.cases()
    for _, commit in (main, branch, unmerged):
        assert repository.git("branch", "--remotes", "--contains", commit, "--list", "origin/*")
    assert repository.git("merge-base", main[1], "origin/main") == main[1]
    assert repository.git("merge-base", branch[1], "origin/integration") == branch[1]
    assert repository.git("merge-base", branch[1], "origin/main") != branch[1]
    assert repository.git("merge-base", unmerged[1], "origin/main") != unmerged[1]
    assert repository.git("merge-base", unmerged[1], "origin/integration") != unmerged[1]


def test_check_uses_configured_persistent_checkout_and_retains_real_result(tmp_path):
    repository = DeliveryRepository.create(tmp_path)
    checkout_base = tmp_path / "persistent-checkouts"
    marker = tmp_path / "observed-checkouts"
    check = OrderCheck(str(repository.path), f'pwd >> "{marker}"; echo 0', CountCheckResult(0))
    commit = repository.git("rev-parse", "HEAD")
    first = check.at(commit, str(repository.path), checkout_base)
    assert check.at(commit, str(repository.path), checkout_base) == first
    actual, = marker.read_text().splitlines()
    assert actual.startswith(str(checkout_base) + "/order-check-")
    assert not tuple(checkout_base.iterdir())
    assert len(check.recorded(commit, str(repository.path))) == 1


def test_delivery_row_declares_comparison_once():
    assert "verify" not in vars(DeliveredCommitParityRow)
    assert DeliveredCommitParityRow.comparison == DeliveredCommitCase.read
