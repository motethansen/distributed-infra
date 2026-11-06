"""Conditions the desk store raises for the API layer to map to HTTP codes."""


class DeskConflict(Exception):
    """The row is not in a state that allows this change (HTTP 409)."""


class DeskPaused(Exception):
    """The desk is paused, so no new command may be claimed (HTTP 423)."""
