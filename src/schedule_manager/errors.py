"""Exception types shared by the CLI, the MCP server, and the model layer."""

from __future__ import annotations


class ScheduleManagerError(Exception):
    """Base class for all schedule-manager domain errors."""


class Unauthenticated(ScheduleManagerError):
    """No usable identity in the current context.

    Raised when no verified token is present, or the token does not resolve to
    a user. Never fall back to a default identity.
    """


class AccountClosed(ScheduleManagerError):
    """The owning user exists but is deactivated (``is_active = false``)."""


class UsernameTaken(ScheduleManagerError):
    """Account creation collided with an existing username."""
