"""Shared, versioned managed-terminal API."""

from importlib import import_module

from .models import PaneBinding, PaneError, PaneRequest, PaneStatus, PaneTransition

API_VERSION = 1

__all__ = [
    "API_VERSION", "FilePaneStore", "HerdrAdapter", "ManagedPaneService",
    "PaneBinding", "PaneError", "PaneRequest", "PaneStatus", "PaneTransition",
    "TmuxAdapter", "ensure", "inspect", "reconcile", "run_visible_sudo", "transition",
]


def __getattr__(name: str):
    if name in {"HerdrAdapter", "TmuxAdapter"}:
        return getattr(import_module(".backends", __name__), name)
    if name in {"FilePaneStore", "ManagedPaneService", "ensure", "inspect", "reconcile", "transition"}:
        return getattr(import_module(".pane", __name__), name)
    if name == "run_visible_sudo":
        return getattr(import_module(".runtime", __name__), name)
    raise AttributeError(name)

