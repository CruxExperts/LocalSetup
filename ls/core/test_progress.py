"""Pytest progress input channel; workers never publish controller counters."""
from __future__ import annotations

import time
from collections import Counter

import pytest

from .test_telemetry import controller_binding, publish_progress


class ProgressReporter:
    def __init__(self, config):
        self.config = config
        self.enabled = not hasattr(config, "workerinput") and controller_binding() is not None
        self.total = None
        self.completed = 0
        self.running = Counter()
        self.unresolved = Counter()
        self.nodes = set()
        self.last_write = 0
        self.distributed = bool(getattr(config.option, "numprocesses", 0))

    def emit(self, *, final=False):
        if not self.enabled or getattr(self.config, "_localsetup_adaptive_bridge", None) is not None:
            return
        now = time.monotonic()
        if not final and now - self.last_write < 1:
            return
        self.last_write = now
        count = getattr(self.config.option, "numprocesses", 0)
        maximum = max(1, count) if type(count) is int else 1
        publish_progress({
            "collection_known": self.total is not None, "total": self.total,
            "completed": self.completed, "running": self.running.total(),
            "pending": None if self.total is None else max(0, self.total - self.completed - self.running.total() - self.unresolved.total()),
            "dispatched": None, "unresolved": self.unresolved.total(),
            "workers": {"active": 0 if final else len(self.nodes) if self.distributed else 1,
                        "maximum": maximum, "target": maximum, "minimum": maximum,
                        "starting": 0, "draining": 0},
            "queues": None, "scheduler": "native" if self.distributed else "serial",
            "ordering": getattr(self.config, "_localsetup_ordering", None),
            "reason": "finished" if final else "fixed_concurrency",
        })

    def pytest_collection_finish(self, session):
        if self.enabled and not self.distributed:
            self.total = len(session.items)
            self.emit()

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        if self.enabled:
            self.total = len(ids)
            self.emit()

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodeready(self, node):
        self.nodes.add(node.gateway.id)
        self.emit()

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        self.nodes.discard(node.gateway.id)
        self.emit()

    def pytest_runtest_logstart(self, nodeid, location):
        if not self.enabled or getattr(self.config, "_localsetup_adaptive_bridge", None) is not None:
            return
        self.running[nodeid] += 1
        if self.unresolved[nodeid]:
            self.unresolved[nodeid] -= 1
            if not self.unresolved[nodeid]:
                del self.unresolved[nodeid]
        self.emit()

    def pytest_runtest_logreport(self, report):
        if not self.enabled or getattr(self.config, "_localsetup_adaptive_bridge", None) is not None:
            return
        if report.when == "teardown":
            if self.running[report.nodeid]:
                self.running[report.nodeid] -= 1
                if not self.running[report.nodeid]:
                    del self.running[report.nodeid]
            self.completed += 1
            self.emit()
        elif report.when == "???" and report.failed:
            if self.running[report.nodeid]:
                self.running[report.nodeid] -= 1
                if not self.running[report.nodeid]:
                    del self.running[report.nodeid]
            self.unresolved[report.nodeid] += 1
            self.emit()

    def pytest_sessionfinish(self, session, exitstatus):
        self.unresolved.update(self.running)
        self.running.clear()
        self.emit(final=True)
