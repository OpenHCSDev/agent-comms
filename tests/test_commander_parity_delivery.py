from agent_comms.commander_parity.delivery import DeliveredCommitCase, DeliveryRepository
from agent_comms.commander_parity.rows import DeliveredCommitParityRow
from agent_comms.goals import CountCheckResult, OrderCheck
from agent_comms.repo import Repo


def test_delivery_graph_is_pushed_and_distinguishes_integration(tmp_path):
    repository = DeliveryRepository.create(tmp_path)
    assert "git" not in vars(DeliveryRepository)
    assert Repo.git(repository.path, "config", "user.name").strip() == "parity"
    assert Repo.git(repository.path, "config", "user.email").strip() == "parity@invalid"
    main, branch, unmerged = repository.cases()
    for _, commit in (main, branch, unmerged):
        assert Repo.git(
            repository.path, "branch", "--remotes", "--contains", commit, "--list", "origin/*"
        ).strip()
    assert Repo.git(repository.path, "merge-base", main[1], "origin/main").strip() == main[1]
    assert (
        Repo.git(repository.path, "merge-base", branch[1], "origin/integration").strip()
        == branch[1]
    )
    assert Repo.git(repository.path, "merge-base", branch[1], "origin/main").strip() != branch[1]
    assert (
        Repo.git(repository.path, "merge-base", unmerged[1], "origin/main").strip() != unmerged[1]
    )
    assert (
        Repo.git(repository.path, "merge-base", unmerged[1], "origin/integration").strip()
        != unmerged[1]
    )


def test_check_uses_configured_persistent_checkout_and_retains_real_result(tmp_path):
    repository = DeliveryRepository.create(tmp_path)
    checkout_base = tmp_path / "persistent-checkouts"
    marker = tmp_path / "observed-checkouts"
    check = OrderCheck(str(repository.path), f'pwd >> "{marker}"; echo 0', CountCheckResult(0))
    commit = Repo.git(repository.path, "rev-parse", "HEAD").strip()
    first = check.at(commit, str(repository.path), checkout_base)
    assert check.at(commit, str(repository.path), checkout_base) == first
    (actual,) = marker.read_text().splitlines()
    assert actual.startswith(str(checkout_base) + "/order-check-")
    assert not tuple(checkout_base.iterdir())
    assert len(check.recorded(commit, str(repository.path))) == 1


def test_delivery_row_declares_comparison_once():
    assert "verify" not in vars(DeliveredCommitParityRow)
    assert DeliveredCommitParityRow.comparison == DeliveredCommitCase.read
