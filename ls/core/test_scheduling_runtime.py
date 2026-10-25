"""Narrow, qualified xdist adapter; scheduling decisions stay on its main thread."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from importlib.metadata import version
import json
import threading
import time

from .test_pressure import AdaptivePolicy, PressureController, sample_pressure
from .test_resources import cgroup_directory


class XdistBridge:
    def __init__(self, config, settings, maximum):
        # Growth and timed events use private xdist APIs; reject an unqualified
        # runtime rather than silently losing coverage on an incompatible update.
        if version("pytest-xdist") != "3.8.0":
            raise RuntimeError("Adaptive scheduling requires qualified pytest-xdist 3.8.0")
        self.config = config
        self.session = config.pluginmanager.getplugin("dsession")
        if self.session is None:
            raise RuntimeError("Adaptive scheduling requires the native xdist controller")
        self.settings = settings
        self.policy = AdaptivePolicy.parse(settings.get("adaptive"))
        self.controller = PressureController(maximum, self.policy)
        self.scheduler = None
        self.stop = threading.Event()
        self.queued = threading.Event()
        self.thread = None
        self.template = None
        self.reason = "starting"
        self.last_transition = None
        self.session.worker_localsetup_pressure = self.on_pressure

    def active_count(self):
        return len(self.session._active_nodes)

    def grow(self):
        if self.session.shuttingdown or self.session.shouldstop or self.active_count() >= self.controller.target:
            return
        if self.template is None:
            self.template = next(iter(self.session._active_nodes), None)
        if self.template is None:
            raise RuntimeError("Adaptive growth has no qualified native worker template")
        self.session._clone_node(self.template)

    def report(self, **payload):
        message = "LocalSetup adaptive tests: " + json.dumps(payload, sort_keys=True)
        if self.session.terminal is not None:
            self.session.terminal.write_line(message)
        else:
            print(message, flush=True)

    def publish_progress(self, *, final=False):
        from .test_telemetry import publish_progress
        scheduler = self.scheduler
        slots = [{"slot": scheduler.slots[node],
                  "pending": len(scheduler.queues[node]),
                  "dispatched": len(scheduler.node2pending[node]),
                  "draining": node in scheduler.draining or node.shutting_down}
                 for node in sorted(scheduler.nodes, key=scheduler.slots.__getitem__)]
        publish_progress({
            "scheduler": "adaptive", "collection_known": scheduler.collection is not None,
            "total": None if scheduler.collection is None else len(scheduler.collection),
            "completed": len(scheduler.completed), "unresolved": len(scheduler.unresolved),
            "pending": len(scheduler.unassigned) + sum(slot["pending"] for slot in slots),
            "dispatched": sum(slot["dispatched"] for slot in slots),
            "workers": {"active": 0 if final else self.active_count(),
                        "maximum": self.controller.maximum, "minimum": self.controller.minimum,
                        "target": self.controller.target,
                        "starting": max(0, self.active_count() - len(scheduler.node2collection)),
                        "draining": sum(slot["draining"] for slot in slots)},
            "queues": slots, "reason": self.reason, "last_transition": self.last_transition,
            "ordering": getattr(self.config, "_localsetup_ordering", None),
        })

    def start(self, scheduler):
        self.scheduler = scheduler
        self.template = next(iter(self.session._active_nodes), None)
        self.report(event="started", maximum=self.controller.maximum,
                    minimum=self.controller.minimum, policy=asdict(self.policy),
                    policy_sha256=hashlib.sha256(json.dumps(self.settings, sort_keys=True).encode()).hexdigest(),
                    memory_metric="cgroup current minus inactive file cache; live host headroom")
        directory = cgroup_directory()

        def sample():
            while not self.stop.wait(self.policy.sample_seconds):
                if self.queued.is_set():
                    continue
                try:
                    payload = {"sample": sample_pressure(directory, self.settings), "now": time.monotonic()}
                except (OSError, ValueError, KeyError) as exc:
                    payload = {"error": type(exc).__name__}
                self.queued.set()
                self.session.queue.put(("localsetup_pressure", payload))
        self.thread = threading.Thread(target=sample, name="test-pressure-sampler", daemon=True)
        self.thread.start()
        self.publish_progress()

    def on_pressure(self, sample=None, now=None, error=None):
        self.queued.clear()
        if self.session.shuttingdown or self.scheduler.tests_finished:
            return
        if error:
            self.session.shouldstop = "Adaptive memory sampling failed: " + error
            self.scheduler.stop_dispatch()
            self.reason = "sampling_failed"
            self.publish_progress()
            return
        previous = self.controller.target
        decision = self.controller.observe(sample.percent, now)
        self.reason = decision.reason or "steady"
        if decision.workers != previous or decision.emergency:
            self.last_transition = {"reason": decision.reason, "target": decision.workers,
                                    "at": time.time()}
            self.report(event=decision.reason, target=decision.workers,
                        actual=self.active_count(), **asdict(sample))
            self.scheduler.set_target(decision.workers)
        else:
            self.scheduler.maybe_grow()
        if decision.emergency:
            self.session.shouldstop = "interrupted_memory_pressure: incomplete tests are not a successful run"
            self.session._max_worker_restart = 0
            self.scheduler.stop_dispatch()
        self.publish_progress()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=self.policy.sample_seconds + 1)
        self.report(event="stopped", completed=len(self.scheduler.completed))
        self.publish_progress(final=True)


def make_scheduler(config, log, settings):
    maximum = config.option.numprocesses
    policy = AdaptivePolicy.parse(settings.get("adaptive"))
    if not policy.enabled or not isinstance(maximum, int) or maximum <= 1:
        return None
    from .test_scheduling import AdaptiveScheduling
    bridge = XdistBridge(config, settings, maximum)
    scheduler = AdaptiveScheduling(config, log, bridge=bridge)
    bridge.start(scheduler)
    config._localsetup_adaptive_bridge = bridge
    return scheduler
