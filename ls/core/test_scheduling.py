"""Adaptive xdist queues: one slot per worker, bounded dispatch, exact coverage."""
from __future__ import annotations

from xdist.scheduler.load import LoadScheduling


class AdaptiveScheduling(LoadScheduling):
    """Only controller-held work moves; dispatched tests retain their owner."""
    def __init__(self, config, log=None, *, bridge=None):
        super().__init__(config, log)
        self.bridge = bridge
        self.queues = {}
        self.slots = {}
        self.draining = set()
        self.unassigned = []
        self.completed = set()
        self.unresolved = set()
        self.target = self.numnodes
        self.next_slot = 0
        self.stopping = False

    @property
    def collection_is_completed(self):
        return self.collection is not None or len(self.node2collection) >= self.numnodes

    @property
    def has_pending(self):
        return bool(self.unassigned or any(self.queues.values()) or any(self.node2pending.values()))

    @property
    def tests_finished(self):
        return self.collection is not None and not self.has_pending

    def add_node(self, node):
        super().add_node(node)
        self.queues[node] = []
        self.slots[node] = self.next_slot
        self.next_slot += 1

    def add_node_collection(self, node, collection):
        if self.collection is not None and list(collection) != self.collection:
            raise RuntimeError("Adaptive worker collection differs; no work was assigned")
        super().add_node_collection(node, collection)

    def eligible(self):
        return sorted((n for n in self.nodes if n not in self.draining
                       and not n.shutting_down and n in self.node2collection),
                      key=self.slots.__getitem__)

    def rebalance(self):
        remaining = sorted([*self.unassigned, *(i for q in self.queues.values() for i in q)])
        self.unassigned = []
        for queue in self.queues.values():
            queue.clear()
        active = self.eligible()
        if not active:
            self.unassigned = remaining
            return
        for index in remaining:
            node = min(active, key=lambda n: (len(self.node2pending[n]) + len(self.queues[n]), self.slots[n]))
            self.queues[node].append(index)

    def dispatch(self):
        if self.stopping or self.collection is None:
            return
        for node in self.eligible():
            count = max(0, 2 - len(self.node2pending[node]))
            selected = self.queues[node][:count]
            del self.queues[node][:count]
            if selected:
                self.node2pending[node].extend(selected)
                node.send_runtest_some(selected)
        # xdist needs a lookahead item or a sentinel before executing a singleton.
        # No controller-held work remains; draining outstanding items is now safe.
        if not self.unassigned and not any(self.queues.values()):
            for node in self.eligible():
                node.shutdown()

    def schedule(self):
        if self.stopping:
            return
        if self.collection is None:
            if not self.collection_is_completed:
                return
            if not self._check_nodes_have_same_collection():
                self.stopping = True
                for node in self.nodes:
                    node.shutdown()
                return
            self.collection = list(next(iter(self.node2collection.values())))
            self.unassigned = list(range(len(self.collection)))
        self.set_target(self.target)

    def mark_test_complete(self, node, item_index, duration=0):
        if item_index in self.completed or item_index not in self.node2pending[node]:
            raise RuntimeError("Adaptive test completion has no unique assigned owner")
        self.node2pending[node].remove(item_index)
        self.completed.add(item_index)
        self.rebalance()
        self.dispatch()

    def check_schedule(self, node, duration=0):
        self.rebalance()
        self.dispatch()

    def remove_node(self, node):
        assigned = self.node2pending.pop(node)
        self.unassigned.extend(self.queues.pop(node))
        self.slots.pop(node)
        self.node2collection.pop(node, None)
        planned = node in self.draining
        self.draining.discard(node)
        crash = None
        if assigned:
            if planned and not getattr(node, "workeroutput", {}).get("exitstatus", 1):
                raise RuntimeError("Retired worker exited before acknowledging its assigned tests")
            assert self.collection is not None
            crash = self.collection[assigned[0]]
            self.unresolved.add(assigned[0])
            self.unassigned.extend(assigned[1:])
        self.rebalance()
        self.dispatch()
        return crash

    def mark_test_pending(self, item):
        # Native crash handling may explicitly request a retry. Never hide a replay.
        index = self.collection.index(item)
        if index in self.completed:
            raise RuntimeError("Cannot replay a completed adaptive test")
        self.unresolved.discard(index)
        self.unassigned.append(index)
        self.rebalance()
        self.dispatch()

    def set_target(self, target):
        if not 1 <= target <= self.numnodes:
            raise ValueError("Adaptive worker target is outside its configured bounds")
        self.target = target
        # Collection is a fixed initial barrier. Record pressure now, but do not
        # retire its participants until a canonical collection can be partitioned.
        if self.collection is None:
            return
        active = self.eligible()
        for node in reversed(active[target:]):
            self.draining.add(node)
            node.shutdown()
        self.rebalance()
        self.dispatch()
        self.maybe_grow()

    def maybe_grow(self):
        if (self.bridge is None or self.stopping or self.draining
                or not (self.unassigned or any(self.queues.values()))):
            return
        if self.bridge.active_count() < self.target:
            self.bridge.grow()

    def stop_dispatch(self):
        self.stopping = True
        for node in self.nodes:
            node.shutdown()
