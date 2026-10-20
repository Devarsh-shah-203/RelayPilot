from playwright.sync_api import sync_playwright
from files.edit_workflow import EditWorkflow
from browser.connection import connect_to_chrome
from browser.chat import BrowserChat
from context.builder import ContextBuilder
from context.session import MemoryContextSession
from files.context import FileContextBuilder
from attachments.integration import (
    build_attachment_prompt,
    plan_referenced_files,
    upload_and_confirm,
)
from memory.manager import MemoryManager
from memory.prompts import build_save_prompt
from files.edit_prompt import build_edit_prompt
from memory.saver import MemorySaver
from workspace.manager import WorkspaceManager
from terminal.command_registry import render_command_help
from terminal.ui import (
    print_auth_required,
    print_header,
    print_memory,
    print_memory_status,
    print_not_ready,
    print_rate_limited,
    print_response,
    print_send_error,
    print_unknown_state,
    print_user_action,
    wait_for_user,
    print_workspace,
    ask_edit_approval,
)


def print_activity(title):
    """Show one clean status banner for an active request."""
    print("\n" + "-" * 56)
    print(f"  {title}")
    print("-" * 56)


def handle_interrupted_state(state, result):
    """
    Handle provider states that require user intervention.
    Returns:
        True  -> caller should continue the main loop.
        False -> state needs no interruption handling.
    """
    if state == "NOT_READY":
        print_not_ready(result)
        wait_for_user(
            "Fix the browser chat page, then press ENTER..."
        )
        return True

    if state == "SEND_ERROR":
        print_send_error(result)
        wait_for_user(
            "Fix the browser and press ENTER..."
        )
        return True

    if state == "RATE_LIMITED":
        print_rate_limited(result)
        wait_for_user(
            "Press ENTER after checking the model page..."
        )
        return True

    if state == "AUTH_REQUIRED":
        print_auth_required(result)
        wait_for_user(
            "Sign in using the browser, then press ENTER..."
        )
        return True

    if state == "USER_ACTION_REQUIRED":
        print_user_action(result)
        wait_for_user(
            "Complete the action in the browser, then press ENTER..."
        )
        return True

    if state == "TIMEOUT_UNKNOWN":
        print_unknown_state()
        wait_for_user(
            "Press ENTER to continue..."
        )
        return True
    return False


def save_memory_from_response(result, memory_saver):
    """Validate and safely persist a completed /save response."""
    try:
        saved_files = memory_saver.save_from_response(result)
    except ValueError as error:
        print("\nMemory proposal was rejected.")
        print(f"Reason: {error}")
        return
    except Exception as error:
        print("\nCould not update project memory.")
        print(f"Error: {error}")
        return
    if not saved_files:
        print("\nNo new important information was identified.")
        return
    print("\nMemory updated successfully:")
    for path in saved_files:
        print(f"  + {path.name}")


def main():
    with sync_playwright() as p:

        # --------------------------------------------------
        # CONNECT TO CHROME
        # --------------------------------------------------

        print_activity("Connecting to Chrome")
        browser = connect_to_chrome(p)
        print("  Connected.")

        # --------------------------------------------------
        # BROWSER CHAT TRANSPORT
        # --------------------------------------------------

        try:
            chat = BrowserChat(browser)
        except RuntimeError as error:
            print(f"\n{error}")
            return
        print("  Chat interface found.")

        # --------------------------------------------------
        # MEMORY / CONTEXT
        # --------------------------------------------------

        memory = MemoryManager()
        workspace = WorkspaceManager()
        context_builder = ContextBuilder()
        file_context_builder = FileContextBuilder(workspace)
        edit_workflow = EditWorkflow(
        workspace_manager=workspace,
        approval_function=ask_edit_approval,
)
        # OFF  -> no memory injection
        # ON   -> include memory once, then snapshot is loaded
        # NEXT -> include memory in the next normal prompt only
        memory_session = MemoryContextSession()
        memory_saver = MemorySaver()
        edit_mode = False
        edit_protocol_sent = False

        # --------------------------------------------------
        # HEADER
        # --------------------------------------------------

        print_header(
            memory_session.get_status()
        )

        # --------------------------------------------------
        # INTERACTIVE LOOP
        # --------------------------------------------------

        while True:
            prompt = input("> ").strip()
            if not prompt:
                continue
            command = prompt.lower()

            # --------------------------------------------------
            # COMMAND HELP (local operation; never sent to the model)
            # --------------------------------------------------

            if command == "/commands" or command.startswith("/commands "):
                category = prompt[len("/commands"):].strip()
                render_command_help(category or None)
                continue

            # --------------------------------------------------
            # EXIT
            # --------------------------------------------------

            if command == "/exit":
                print("\nGoodbye.")
                break

            # --------------------------------------------------
            # EDIT MODE
            # --------------------------------------------------

            if command == "/edit":
                if edit_mode:
                    print("\nAlready in EDIT MODE.")
                else:
                    edit_mode = True
                    print("\nEDIT MODE: ON")
                    print("  Describe the changes you want to make.")
                    print("  Use <workspace_id/relative/path> for files.")
                    print("  Type /cancel to leave edit mode.")
                continue

            # --------------------------------------------------
            # CANCEL EDIT MODE
            # --------------------------------------------------

            if command == "/cancel":
                if edit_mode:
                    edit_mode = False
                    edit_protocol_sent = False
                    print("\nEDIT MODE: OFF")
                else:
                    print("\nNot currently in EDIT MODE.")
                continue

            # --------------------------------------------------
            # WORKSPACE
            # --------------------------------------------------

            if command == "/workspace":
                print_workspace(
                    workspace.get_status()
                )
                continue

            # --------------------------------------------------
            # WORKSPACE ADD
            # --------------------------------------------------

            if command.startswith("/workspace add"):
                raw_path = prompt[
                    len("/workspace add"):
                ].strip()
                if not raw_path:
                    print(
                        "\nUsage:"
                    )
                    print(
                        "  /workspace add <directory>"
                    )
                    continue
                # Allow paths with spaces wrapped in quotes.
                path = raw_path.strip('"')
                try:
                    entry = workspace.add_directory(
                        path
                    )
                except (ValueError, TypeError) as error:
                    print(
                        "\nCould not add workspace directory."
                    )
                    print(
                        f"Reason: {error}"
                    )
                    continue
                print(
                    "\nWorkspace directory added."
                )
                print(
                    f"  [{entry['id']}] "
                    f"{entry['path']}"
                )
                print()
                continue

            # --------------------------------------------------
            # WORKSPACE REMOVE
            # --------------------------------------------------

            if command.startswith("/workspace remove"):
                raw_id = prompt[
                    len("/workspace remove"):
                ].strip()
                if not raw_id:
                    print(
                        "\nUsage:\n"
                        "  /workspace remove <id>"
                    )
                    continue
                if not raw_id.isdigit():
                    print(
                        "\nWorkspace ID must be a number."
                    )
                    continue
                workspace_id = int(raw_id)
                try:
                    removed = workspace.remove_directory(
                        workspace_id
                    )
                except (ValueError, TypeError) as error:
                    print(
                        "\nCould not remove workspace directory."
                    )
                    print(
                        f"Reason: {error}"
                    )
                    continue
                print(
                    "\nWorkspace directory removed."
                )
                print(
                    f"  [{removed['id']}] "
                    f"{removed['path']}"
                )
                print()
                continue

            # --------------------------------------------------
            # WORKSPACE CLEAR
            # --------------------------------------------------

            if command == "/workspace clear":
                directories = workspace.list_directories()
                if not directories:
                    print(
                        "\nNo workspace directories are configured."
                    )
                    continue
                count = len(directories)
                workspace.clear()
                print(
                    f"\nWorkspace cleared. "
                    f"{count} director{'y' if count == 1 else 'ies'} removed."
                )
                continue

            # --------------------------------------------------
            # MEMORY USE
            # --------------------------------------------------

            if (
                command == "/memory use"
                or command.startswith("/memory use ")
            ):
                memory_session.use_once()

                # Standalone command: arm one-time memory injection.
                if command == "/memory use":
                    print_activity("Memory mode: NEXT")
                    print(
                        "  Project memory will be included "
                        "in the next normal prompt only."
                    )
                    print_memory_status(
                        memory_session.get_status()
                    )
                    print()
                    continue

                # Inline command: attach memory to this question only.
                prompt = prompt[len("/memory use"):].strip()
                command = "__inline_prompt__"

                print_activity("Memory mode: NEXT")
                if edit_mode:
                    print(
                        "  EDIT MODE suppresses memory injection; "
                        "one-time memory use remains pending for the next normal prompt."
                    )
                else:
                    print(
                        "  Project memory will be included in this request only."
                    )
                print_memory_status(
                    memory_session.get_status()
                )
                print()

            # --------------------------------------------------
            # MEMORY ON
            # --------------------------------------------------

            if (
                command == "/memory on"
                or command.startswith("/memory on ")
            ):
                memory_session.enable()

                # Standalone command: enable memory for the next normal prompt.
                if command == "/memory on":
                    print_activity("Memory mode: ON")
                    print(
                        "  The current project-memory snapshot "
                        "will be included in the next normal prompt."
                    )
                    print(
                        "  It will not be resent with every prompt."
                    )
                    print_memory_status(
                        memory_session.get_status()
                    )
                    print()
                    continue

                # Inline command: enable memory and send this question now.
                prompt = prompt[len("/memory on"):].strip()
                command = "__inline_prompt__"

                print_activity("Memory mode: ON")
                if edit_mode:
                    print(
                        "  EDIT MODE suppresses memory injection; "
                        "memory mode remains ON for the next normal prompt."
                    )
                else:
                    print(
                        "  The current project-memory snapshot "
                        "will be included in this request."
                    )
                print(
                    "  It will not be resent with every prompt."
                )
                print_memory_status(
                    memory_session.get_status()
                )
                print()

            # --------------------------------------------------
            # MEMORY OFF
            # --------------------------------------------------

            if command == "/memory off":
                memory_session.disable()
                print_activity("Memory mode: OFF")
                print(
                    "  Future prompts will not receive "
                    "project memory."
                )
                print_memory_status(
                    memory_session.get_status()
                )
                print()
                continue

            # --------------------------------------------------
            # MEMORY REFRESH
            # --------------------------------------------------

            if (
                command == "/memory refresh"
                or command.startswith("/memory refresh ")
            ):
                memory_session.refresh()

                # Command by itself: queue memory for the next prompt.
                if command == "/memory refresh":
                    print_activity("Memory refresh queued")
                    print(
                        "  The current project-memory snapshot "
                        "will be included in the next normal prompt."
                    )
                    print_memory_status(
                        memory_session.get_status()
                    )
                    print()
                    continue

                # Command followed by text: refresh memory and send
                # only the remaining text as the user's actual prompt.
                prompt = prompt[len("/memory refresh"):].strip()
                command = "__inline_prompt__"

                print_activity("Memory refresh requested")
                if edit_mode:
                    print(
                        "  EDIT MODE suppresses memory injection; "
                        "the refresh remains pending for the next normal prompt."
                    )
                else:
                    print(
                        "  The refreshed project-memory snapshot "
                        "will be included in this request."
                    )
                print_memory_status(
                    memory_session.get_status()
                )
                print()

            # --------------------------------------------------
            # SAVE MEMORY
            # --------------------------------------------------

            if command == "/save":
                print_activity("Saving project memory")
                print("  Reviewing current conversation...")
                save_prompt = build_save_prompt()
                print("  Sending memory review to model...")
                state, result = chat.send_prompt(
                    save_prompt
                )
                if handle_interrupted_state(
                    state,
                    result
                ):
                    continue
                if state != "COMPLETED":
                    print("\nMemory save did not complete.")
                    continue
                print("\n" + "-" * 56)
                print("  Model memory review")
                print("-" * 56)
                print(result)
                save_memory_from_response(
                    result,
                    memory_saver
                )
                print()
                continue

            # --------------------------------------------------
            # DETERMINE MEMORY USAGE
            # --------------------------------------------------

            include_memory = (
                not edit_mode
                and memory_session.should_include_memory()
            )

            # --------------------------------------------------
            # BUILD MODEL PROMPT
            # --------------------------------------------------

            try:
                # Plan file delivery before reading file contents. PDF/image/spreadsheet
                # references must use browser attachments rather than UTF-8 inline reads.
                # Small text requests retain the existing inline-context behavior unless
                # AI_GATEWAY_FORCE_ATTACHMENTS=1 is set for an upload test.
                memory_characters = 0
                if include_memory:
                    try:
                        memory_values = context_builder.memory.read_all()
                        memory_characters = sum(
                            len(value) for value in memory_values.values()
                            if isinstance(value, str) and value.strip()
                        )
                    except Exception:
                        # ContextBuilder remains responsible for surfacing real memory
                        # read errors; this size estimate only influences delivery mode.
                        memory_characters = 0

                attachment_plan = plan_referenced_files(
                    prompt,
                    workspace,
                    memory_characters=memory_characters,
                    edit_mode=edit_mode,
                )
                model_prompt = prompt
                if attachment_plan.uses_attachments:
                    upload_decision = upload_and_confirm(
                        attachment_plan.paths,
                        chat.page,
                    )
                    # Critical safety gate: never submit a request with unverified or
                    # unresolved files unless the user explicitly chose manual or
                    # verified-only recovery inside upload_and_confirm().
                    if not upload_decision.proceed:
                        print("\nRequest was not submitted because file attachments were not confirmed.")
                        continue
                    model_prompt = build_attachment_prompt(
                        prompt,
                        attachment_plan.references,
                        upload_decision.attached_paths,
                        upload_decision.report,
                    )
                    file_context = []
                else:
                    file_context = file_context_builder.build(prompt)

                if edit_mode:
                    final_prompt = build_edit_prompt(
                        prompt,
                        file_context,
                        include_instructions=not edit_protocol_sent,
                    )
                else:
                    final_prompt = context_builder.build(
                        model_prompt,
                        include_memory=include_memory,
                        file_context=file_context
                    )
            except Exception as error:
                print("\nCould not prepare prompt context or attachments.")
                print(error)
                continue

            # --------------------------------------------------
            # SEND TO MODEL
            # --------------------------------------------------

            print_activity(
                "Sending request to model"
            )
            if include_memory:
                print("  Project memory snapshot: attached")
            state, result = chat.send_prompt(
                final_prompt
            )
            if edit_mode and state not in {"NOT_READY", "SEND_ERROR"}:
                edit_protocol_sent = True

            # --------------------------------------------------
            # MARK MEMORY AS SENT
            # --------------------------------------------------

            if (
                include_memory
                and state not in [
                    "NOT_READY",
                    "SEND_ERROR",
                ]
            ):
                memory_session.mark_memory_sent()

            # --------------------------------------------------
            # RESPONSE / INTERRUPTION
            # --------------------------------------------------

            if state == "COMPLETED":
                if edit_mode:
                    try:
                        edit_results = edit_workflow.run(result)
                    except Exception as exc:
                        print(f"\nEdit workflow error: {exc}")
                else:
                    print_response(result)
            elif not handle_interrupted_state(
                state,
                result
            ):
                print("\nUnhandled model state.")

            # --------------------------------------------------
            # MEMORY STATUS
            # --------------------------------------------------

            print_memory_status(
                memory_session.get_status()
            )
            print()


if __name__ == "__main__":
    main()
