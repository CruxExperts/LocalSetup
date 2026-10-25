"""Reproducible randomized case order and balanced CI queue assignment."""
from __future__ import annotations

import hashlib
import os
import secrets


MAX_SEED = 2**31 - 1


def validate_seed(value):
    if type(value) is not int or not 0 <= value <= MAX_SEED:
        raise ValueError("test shuffle seed must be an integer between 0 and 2147483647")
    return value


def select_seed(settings, *, explicit=None, environ=None, sharded=False):
    env = os.environ if environ is None else environ
    if explicit is not None:
        return validate_seed(explicit), "command_line"
    if "LOCALSETUP_TEST_SEED" in env:
        return validate_seed(int(env["LOCALSETUP_TEST_SEED"])), "environment"
    if settings.get("shuffle_seed") is not None:
        return validate_seed(settings["shuffle_seed"]), "configuration"
    if env.get("GITHUB_RUN_ID"):
        # Attempt must NOT participate: rerunning only failed jobs must preserve
        # the partition used by successful jobs in the same GitHub run.
        value = int.from_bytes(hashlib.sha256(env["GITHUB_RUN_ID"].encode()).digest()[:4]) & MAX_SEED
        return value, "github_run"
    if sharded:
        # Non-GitHub matrix runners must supply one shared seed for a fresh mix.
        # A stable fallback is safer than independently randomized partitions.
        return 0, "shared_shard_default"
    return secrets.randbits(31), "random"


def partition(items, *, seed, shard=0, count=1, shuffle=True):
    """Randomize by stable identity, then deal evenly across queue slots."""
    validate_seed(seed)
    if not 1 <= count <= 32 or not 0 <= shard < count:
        raise ValueError("invalid test partition")
    prefix = seed.to_bytes(4, "big")
    mixed = sorted(items, key=lambda item: (
        hashlib.sha256(prefix + b"\0" + item.nodeid.encode()).digest(), item.nodeid))
    selected = mixed[shard::count]
    selected_objects = {id(item) for item in selected}
    excluded = [item for item in items if id(item) not in selected_objects]
    if not shuffle:
        selected = [item for item in items if id(item) in selected_objects]
    return selected, excluded
