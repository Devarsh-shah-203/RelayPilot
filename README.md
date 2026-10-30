# RelayPilot

**A terminal-first way to work with browser-based AI on your own codebase.**

RelayPilot connects your terminal to an AI chat session already open in Chrome. It helps you ask questions about a project, provide relevant file context, keep reusable project notes, and propose code changes without having to manually copy and paste everything into a browser chat.

The aim is to make browser-based AI more useful for real development work—while keeping the developer in control of which context is shared and which file changes are applied.

## Table of contents

* [Why RelayPilot?](#why-relaypilot)
* [What can you do with it?](#what-can-you-do-with-it)

  * [Ask questions about your code](#ask-questions-about-your-code)
  * [Make targeted code changes](#make-targeted-code-changes)
  * [Maintain reusable project knowledge](#maintain-reusable-project-knowledge)
  * [Work across more than one project](#work-across-more-than-one-project)
* [How it fits together](#how-it-fits-together)
* [Command overview](#command-overview)
* [Design principles](#design-principles)
* [Current status and limitations](#current-status-and-limitations)
* [Project structure](#project-structure)
* [Where setup instructions belong](#where-setup-instructions-belong)
* [License](#license)

## Why RelayPilot?

Using an AI chat while coding often means repeatedly switching windows, pasting code, explaining the project again, and manually transferring suggested changes back into files.

RelayPilot brings several of those steps into one terminal workflow:

* **Work from your terminal.** Send prompts to the chat session in your browser without manually moving every request between the terminal and browser.
* **Give AI relevant project context.** Register your own project directories and refer to specific files in your prompt.
* **Spend less time repeating project background.** Keep project knowledge in local memory files and choose when a memory snapshot should be included in a request.
* **Review changes before they reach your files.** Use the explicit edit workflow to inspect a proposed change and its explanation, then approve or reject it.
* **Keep projects organized.** Configure multiple workspaces and reference files using stable numeric IDs.
* **Reuse browser-based AI access.** The current workflow uses an existing signed-in Chrome session rather than requiring an AI API key from RelayPilot itself. The full-screen Textual interface delegates synchronous Playwright operations to a dedicated runtime thread so the terminal can remain responsive.

## What can you do with it?

### Ask questions about your code

Register a project folder as a workspace, then reference a file in a natural-language prompt. The gateway can attach the referenced file's contents as context, so you can ask about implementation details without manually pasting the file.

**Example:**

```text
Explain the authentication flow in <1/src/auth.js>
```

Here, `1` is the workspace ID and `src/auth.js` is the path relative to that workspace. File references use the form `<workspace_id/relative/path>` and can be combined in one prompt.

### Make targeted code changes

Enter edit mode, describe the change, and reference the file you want to modify. The gateway uses a structured edit workflow to validate the model's proposal and show the intended change for approval before applying it.

**Example:**

```text
/edit
Add input validation to <1/src/auth.js> and preserve the existing behavior.
```

Review every proposed diff. RelayPilot is designed to keep you in the approval loop; it does not make model-generated code automatically trustworthy.

### Maintain reusable project knowledge

Project memory lets you keep durable notes about architecture, decisions, conventions, and progress on your own machine. You control when that context is supplied to a normal prompt, rather than sending all local memory files with every request.

The `/save` command asks the current browser conversation to identify useful long-term project knowledge and attempts to save a validated proposal to local memory files.

### Work across more than one project

Configure multiple workspace directories. The gateway assigns each one a numeric ID, which you can use to refer to files unambiguously.

```text
/workspace add "D:\Projects\backend"
/workspace add "D:\Projects\frontend"
/workspace
```

Workspace configuration only registers directories for gateway workflows; removing a workspace does not delete its files.

## How it fits together

At a high level, RelayPilot connects a few pieces:

* **Browser transport** connects to an already-running Chrome session using Playwright.
* **Prompt and context assembly** combines your request with the file context you explicitly reference and any memory snapshot that is enabled for that request.
* **Project memory** manages local notes and the rules for injecting those notes into prompts.
* **Workspace and file tools** resolve file references and prepare structured edits.
* **Edit review** validates the proposed change and asks for approval before writing it.

The intent is to keep browser interaction, context handling, memory, workspace management, and file editing as separate responsibilities so each area can evolve without unnecessarily disturbing the others.

## Command overview

Commands currently available in the CLI include:

| Command                      | What it helps you do                                                                                             |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `/workspace`                 | See configured workspaces and their status.                                                                      |
| `/workspace add <directory>` | Register a project directory.                                                                                    |
| `/workspace remove <id>`     | Remove a directory from the workspace configuration.                                                             |
| `/workspace clear`           | Clear all workspace entries without deleting project files.                                                      |
| `/edit`                      | Enter the controlled file-edit workflow.                                                                         |
| `/cancel`                    | Leave edit mode.                                                                                                 |
| `/memory use`                | Include project memory with the next normal prompt only.                                                         |
| `/memory on`                 | Enable memory mode.                                                                                              |
| `/memory off`                | Disable future memory injection.                                                                                 |
| `/memory refresh`            | Queue the current memory snapshot for the next prompt.                                                           |
| `/save`                      | Extract durable knowledge from the current browser conversation and attempt to save a validated memory proposal. |
| `/exit`                      | Exit the CLI.                                                                                                    |

`/commands` provides an in-terminal command reference with syntax, descriptions, and examples. Focused help such as `/commands memory` or `/commands workspace` narrows the reference.

## Design principles

* **Developer control:** Memory injection is explicit, and proposed file edits require review.
* **Local-first project state:** Workspace settings and project memory are kept locally by default.
* **Explicit file references:** The gateway should use files you identify, not guess which project file you meant.
* **Focused responsibilities:** Browser transport, context assembly, memory, workspaces, and editing are separate components.
* **Provider-agnostic direction:** The application and terminal contracts are provider-neutral; provider-specific browser workflows must still be implemented and validated individually.

## Current status and limitations

RelayPilot is an active development project, not a finished or universally compatible coding agent.

* The current prompt/response workflow is validated with ChatGPT in Chrome. The UI can identify Claude, Gemini, and other browser hosts, but does not yet enable their prompt workflows.
* Browser interaction depends on the chat website's interface, which can change and break automation.
* Chrome must remain open and reachable through remote debugging, with a valid signed-in session when required.
* Browser service usage limits and account restrictions still apply. RelayPilot does not bypass them.
* The Textual terminal is integrated with the existing synchronous Playwright workflow through a dedicated runtime thread. Response text is updated progressively from browser DOM snapshots; this is not token-level SSE streaming. Live CDP operation must still be verified on the machine running Chrome.
* Model responses and suggested edits can be incorrect. Review changes and use version control or backups for important work.

## Project structure

```text
RelayPilot/
├── browser/       # Chrome connection and browser-chat transport
├── context/       # Prompt/context assembly and memory-session behavior
├── files/         # File references, edit protocol, validation, diffs and writes
├── memory/        # Project-memory prompts, parsing, management and saving
├── application/   # Commands, request state and response storage
├── runtime/       # Dedicated Playwright owner thread
├── terminal/      # Textual UI, widgets, dialogs and command palette
├── workspace/     # Workspace registration, validation and persistence
├── main.py        # Full-screen Textual application
├── main_legacy.py # Previous print-based CLI for rollback
└── requirements.txt
```

## Where setup instructions belong

This README focuses on what RelayPilot does and how it can help. Detailed installation, Chrome configuration, and environment-specific troubleshooting will be documented separately in `SETUP.md`.

## License

No license file is currently present in the repository. Until a license is added, do not assume that the code is available for unrestricted reuse, redistribution, or modification.


## Running the terminal application

Install dependencies and start the configured Chrome remote-debugging session as described in [SETUP.md](SETUP.md), then run:

```bash
python main.py
```

RelayPilot opens a full-screen Textual terminal. Press **Enter to send** and **Shift+Enter to insert a newline** (or Alt+N / the New line button if VS Code does not pass Shift+Enter correctly). See `VSCODE_TERMINAL_SHORTCUTS.md` for user-level VS Code keybindings that also restore Shift+Enter and Alt+Enter. To open commands, help, or quit, use the clickable header controls or type `/commands`, `/help`, or `/exit`. `Alt+P`, `Alt+H`, and `Alt+Q` are keyboard alternatives when VS Code intercepts `Ctrl+P`, `F1`, or `Ctrl+Q`. Use `Escape` to stop the active request (including while an approval or recovery dialog is open) and `Ctrl+R` to reconnect to Chrome. The header and status bar identify the detected provider/site (not necessarily the exact model SKU); Claude, Gemini, and other hosts are identified but their chat workflows remain disabled until implemented and validated. Escape closes Help or the command palette; elsewhere it cancels the active request.


### Edit review in the terminal

When `/edit` is enabled, model-generated `<AI_EDIT>` protocol text is **not** displayed as a normal assistant answer. RelayPilot keeps the raw response internally, validates it with the existing edit parser and validator, and opens a scrollable diff-review dialog for each proposed change. Use Approve/Reject (buttons or `A`/`R`). The applied/rejected outcome, or a parsing/validation error, remains visible in the conversation. Invalid proposals are never auto-applied.

### Project name and configuration migration

New workspace settings are stored in `.relaypilot/workspace.json`. On first use, RelayPilot copies an existing `.ai-gateway/workspace.json` into the new location, preserving workspace IDs and keeping the original for rollback. New environment variables use `RELAYPILOT_*`; older `AI_GATEWAY_*` names remain accepted for backward compatibility. Existing `<AI_EDIT>` tags are protocol identifiers and are **not** renamed.


### Readable streamed replies and edit review

The browser extraction layer now preserves bullet/numbered list markers, Markdown
tables, headings and fenced code, rather than flattening them into bare text.
The completed response is rendered with Rich Markdown. Request status (preparing,
submitting, waiting, generating) is shown temporarily beneath the originating
user message and disappears shortly after completion.

In `/edit` mode, model-generated `<AI_EDIT>` protocol payloads remain hidden in
the conversation but are preserved for the existing parser and validator.
Successful proposals open a diff-approval dialog; malformed proposals show a
persistent, actionable error. No change is applied without approval.
