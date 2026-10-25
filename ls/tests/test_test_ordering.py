"""Random mixing must remain reproducible and preserve cross-job coverage."""
from types import SimpleNamespace

from ls.core.test_ordering import partition, select_seed


def test_seeded_partitions_are_balanced_and_cover_reordered_collection_once():
    items = [SimpleNamespace(nodeid=f"module{i // 20}::case{i}") for i in range(1001)]
    queues = [partition(items, seed=123, shard=i, count=12)[0] for i in range(12)]
    assert max(map(len, queues)) - min(map(len, queues)) <= 1
    assert sorted(item.nodeid for queue in queues for item in queue) == sorted(item.nodeid for item in items)
    assert queues[0] == partition(list(reversed(items)), seed=123, shard=0, count=12)[0]
    assert queues[0] != partition(items, seed=124, shard=0, count=12)[0]
    # A contiguous family does not stay together as a block in the shuffled order.
    mixed = partition(items, seed=123)[0]
    assert len({item.nodeid.split("::")[0] for item in mixed[:20]}) > 10
    assert partition(items, seed=123, shuffle=False)[0] == items


def test_ci_rerun_preserves_shared_seed_and_new_run_changes_it():
    first = select_seed({}, environ={"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}, sharded=True)
    retry = select_seed({}, environ={"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2"}, sharded=True)
    another = select_seed({}, environ={"GITHUB_RUN_ID": "124"}, sharded=True)
    assert first == retry
    assert first != another
    assert select_seed({}, environ={}, sharded=True) == (0, "shared_shard_default")
    assert select_seed({}, explicit=first[0], environ={})[0] == first[0]
