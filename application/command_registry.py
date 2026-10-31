"""Canonical command metadata and in-terminal help for RelayPilot.

Keep CLI command descriptions and examples here rather than duplicating the help
text in main.py or terminal/ui.py. Command execution is routed through the application layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class CommandSpec:
    name: str
    category: str
    description: str
    usage: str
    examples: tuple[str, ...] = ()
    show_in_header: bool = False
    header_hint: str = ""


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        name="/commands",
        category="general",
        description="Show the command reference. Optionally filter by category.",
        usage="/commands [general|workspace|memory|files|edit]",
        examples=("/commands", "/commands workspace", "/commands files"),
        show_in_header=True,
        header_hint="View command help",
    ),
    CommandSpec(
        name="/help",
        category="general",
        description="Open the keyboard shortcuts and full command reference.",
        usage="/help",
        examples=("/help",),
    ),
    CommandSpec(
        name="/quit",
        category="general",
        description="Exit RelayPilot (alias for /exit).",
        usage="/quit",
        examples=("/quit",),
    ),
    CommandSpec(
        name="/exit",
        category="general",
        description="Exit RelayPilot.",
        usage="/exit",
        examples=("/exit",),
        show_in_header=False,
        header_hint="Quit",
    ),
    CommandSpec(
        name="/workspace",
        category="workspace",
        description="List configured workspace IDs, paths, and availability.",
        usage="/workspace",
        examples=("/workspace",),
        show_in_header=False,
        header_hint="List workspaces",
    ),
    CommandSpec(
        name="/workspace add <directory>",
        category="workspace",
        description="Register an existing directory. Quote the path if it contains spaces.",
        usage="/workspace add <directory>",
        examples=(r'/workspace add "D:\Projects\backend"',),
    ),
    CommandSpec(
        name="/workspace remove <id>",
        category="workspace",
        description="Remove a workspace entry from the gateway configuration. Files on disk are not deleted.",
        usage="/workspace remove <id>",
        examples=("/workspace remove 2",),
    ),
    CommandSpec(
        name="/workspace clear",
        category="workspace",
        description="Clear all workspace entries. Project directories and files are not deleted.",
        usage="/workspace clear",
        examples=("/workspace clear",),
    ),
    CommandSpec(
        name="/memory", category="memory",
        description="List the registered Markdown memory files for this project. Bare memory commands also work.",
        usage="memory", examples=("memory", "/memory"),
    ),
    CommandSpec(
        name="/memory refresh [question]", category="memory",
        description="Include registered memory once in the next model request, including /edit. An optional question is sent now.",
        usage="memory refresh [question]", examples=("memory refresh", "memory refresh Explain our architecture."),
    ),
    CommandSpec(
        name="/memory add [name]", category="memory",
        description="Create and register Markdown memory with optional initial content. The .md extension is added automatically.",
        usage="memory add [name]", examples=("memory add architecture", "memory add"),
    ),
    CommandSpec(
        name="/memory renew", category="memory",
        description="Register new .ai Markdown files and report unsupported or missing files.",
        usage="memory renew", examples=("memory renew",),
    ),
    CommandSpec(
        name="/save",
        category="memory",
        description="Ask the current browser conversation to identify durable project knowledge and save a validated proposal locally.",
        usage="/save",
        examples=("/save",),
        show_in_header=False,
        header_hint="Save project knowledge",
    ),
    CommandSpec(
        name="/edit",
        category="edit",
        description="Enter edit mode. Optionally select an existing workspace for new files. CREATE and MODIFY both require approval.",
        usage="/edit [workspace_id]",
        examples=("/edit", "/edit 1", "Add validation to <1/src/auth.js> while preserving existing behavior."),
        show_in_header=False,
        header_hint="Edit with approval",
    ),
    CommandSpec(
        name="/cancel",
        category="edit",
        description="Leave edit mode. This does not undo changes already approved and applied.",
        usage="/cancel",
        examples=("/cancel",),
    ),
)

HELP_CATEGORIES = ("general", "workspace", "memory", "files", "edit")
COMMANDS_BY_NAME = {item.name: item for item in COMMANDS}


def get_header_commands() -> tuple[CommandSpec, ...]:
    """Return the compact set of command entries shown at startup.

    Startup should teach users how to discover help, not duplicate the full
    command reference in the banner. Detailed help remains available through
    ``/commands``.
    """
    return tuple(item for item in COMMANDS if item.show_in_header)


def render_command_help(
    category: str | None = None,
    *,
    output: Callable[..., None] = print,
) -> None:
    """Print all command help or the subset for one known category."""
    selected = (category or "").strip().lower()
    if selected and selected not in HELP_CATEGORIES:
        output("Unknown help category.")
        output("Usage: /commands [general|workspace|memory|files|edit]")
        output("Available categories: " + ", ".join(HELP_CATEGORIES))
        return

    output("\n" + "=" * 72)
    output("RELAYPILOT — COMMAND REFERENCE")
    output("=" * 72)
    output(f"Category: {selected}" if selected else "All commands")
    output("-" * 72)

    for spec in COMMANDS:
        if selected and selected != spec.category:
            continue
        output(f"\n{spec.usage}")
        output(f"  {spec.description}")
        if spec.examples:
            output("  Example(s):")
            for example in spec.examples:
                output(f"    {example}")

    if not selected or selected == "files":
        output("\nFILE REFERENCES (put these inside the prompt you want to send)")
        output("-" * 72)
        output("<workspace_id/relative/path>")
        output("  Include one file as context. Example: Explain <1/src/main.py>")
        output("<workspace_id/folder/filename>")
        output("  Include a file inside a workspace subfolder.")
        output("<workspace_id/all>")
        output("  Collect eligible files recursively from a workspace; each file uses the normal inline/attachment policy.")
        output("<workspace_id/./all>")
        output("  Select a literal file named all; ./ disables bulk interpretation. Numeric path segments are ordinary paths.")
        output("  Read a folder named all with <workspace_id/all/all> (the last all is the selector).")
        output("<workspace_id/folder/all>")
        output("  Collect eligible files recursively from that subfolder; each file uses the normal inline/attachment policy.")
        output("  Bulk collection skips generated folders, secrets/local config, unsupported/binary files,")
        output("  and Jupyter notebooks. Supported images, documents, spreadsheets, and archives may be attached.")
        output("  Safety caps: RELAYPILOT_BULK_MAX_FILES (default 200) and RELAYPILOT_BULK_MAX_BYTES (default 100 MiB).")
        output("  Reference a notebook individually (for example <1/notebooks/eda.ipynb>) to select its cells.")
        output("  Explicit eligible references upload first, including small source files and /edit requests.")
        output("  Configure RELAYPILOT_ATTACHMENT_CHAR_THRESHOLD, RELAYPILOT_ATTACHMENT_SIZE_MB, and RELAYPILOT_INLINE_TOKEN_THRESHOLD.")
        output("  Inline fallback uses complete eligible text/source or approved notebook content, within the shared request budget.")

    output("\nUse /commands workspace, /commands memory, /commands files, or /commands edit")
    output("to view focused help. File references are syntax inside prompts, not slash commands.")
    output("=" * 72)
