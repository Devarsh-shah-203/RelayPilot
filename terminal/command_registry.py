"""Compatibility exports for the canonical application command registry.

Command metadata belongs to the application layer so the CLI, help screen and
other future frontends can share one registry without application->terminal
imports. Keep this module as a stable import path for older code and tests.
"""

from application.command_registry import (
    COMMANDS,
    COMMANDS_BY_NAME,
    HELP_CATEGORIES,
    CommandSpec,
    get_header_commands,
    render_command_help,
)

__all__ = [
    "COMMANDS",
    "COMMANDS_BY_NAME",
    "HELP_CATEGORIES",
    "CommandSpec",
    "get_header_commands",
    "render_command_help",
]
