"""RelayPilot full-screen terminal application backed by GatewayRuntime."""

from __future__ import annotations

import asyncio
from time import monotonic

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Static

from application.contracts import (
    AppNotice,
    AttachmentStatusChanged,
    CancelRequest,
    CommandCompleted,
    CommandFailed,
    EditApprovalRequired,
    EditModeChanged,
    EditProposalStarted,
    EditReviewResult,
    ExecuteCommand,
    HelpRequested,
    MemoryStatusChanged,
    RequestCancelled,
    RequestCompleted,
    RequestFailed,
    RequestStateChanged,
    ResponseDelta,
    ResponseRevised,
    ResponseStarted,
    RuntimeConnectionFailed,
    RuntimeEventType,
    RuntimeReady,
    RuntimeStopped,
    SubmitPrompt,
    UserActionRequired,
    UserInputRequested,
    WorkspaceStatusChanged,
    new_request_id,
)
from runtime.gateway_runtime import GatewayRuntime
from terminal.dialogs.user_input import UserInputScreen
from terminal.runtime_messages import RuntimeEventMessage
from terminal.screens.command_palette import CommandPaletteScreen
from terminal.screens.help import HelpScreen
from terminal.widgets.composer import PromptComposer, PromptInput
from terminal.widgets.conversation import ConversationView
from terminal.widgets.message import ConversationMessage
from terminal.widgets.edit_review import EditReviewActions, InlineEditReview
from terminal.widgets.status_bar import StatusBar


class RelayPilotApp(App[None]):
    """Full-screen TUI; browser work is delegated to its owner thread."""

    TITLE = "RelayPilot"
    SUB_TITLE = ""
    CSS_PATH = "theme.tcss"
    ENABLE_COMMAND_PALETTE = False  # Ctrl+P opens RelayPilot's own command palette.

    BINDINGS = [
        Binding("escape", "cancel_request", "Stop", show=False, priority=True),
        # VS Code may intercept Ctrl+P, Ctrl+Q and F1 before the terminal sees them.
        # Alt bindings, clickable controls and slash commands are alternatives.
        Binding("alt+p", "open_command_palette", "Commands", show=True, priority=True),
        Binding("alt+h", "show_help", "Help", show=True, priority=True),
        Binding("alt+q", "request_exit", "Quit", show=True, priority=True),
        Binding("ctrl+p", "open_command_palette", "Commands", show=False, priority=True),
        Binding("ctrl+r", "reconnect_browser", "Reconnect", show=False),
        Binding("f1", "show_help", "Help", show=False),
        Binding("ctrl+q", "request_exit", "Quit", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.runtime = GatewayRuntime(self.post_runtime_event)
        self._active_request_id: str | None = None
        self._response_widgets: dict[str, ConversationMessage] = {}
        self._request_started_at: dict[str, float] = {}
        self._provider = "Not connected"
        self._runtime_shutdown_started = False
        self._edit_mode = False
        self._edit_request_ids: set[str] = set()
        self._edit_review_failed_requests: set[str] = set()
        self._user_request_widgets: dict[str, ConversationMessage] = {}
        self._review_widgets: dict[str, InlineEditReview] = {}
        self._review_request_ids: dict[str, list[str]] = {}
        self._pending_review_id: str | None = None
        self._pending_review_request: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="app-shell"):
            with Horizontal(id="topbar"):
                yield Static("RELAYPILOT", id="brand", markup=False)
                yield Static("CONNECTING TO CHROME…", id="connection-label", markup=False)
                yield Button("Commands", id="commands-button", classes="nav-action")
                yield Button("Help", id="help-button", classes="nav-action")
                yield Button("Quit", id="quit-button", classes="nav-action")
            yield ConversationView()
            yield EditReviewActions(id="edit-review-actions")
            yield PromptComposer(id="composer")
            yield StatusBar()
            yield Footer()

    async def on_mount(self) -> None:
        self.query_one(PromptInput).focus()
        self.query_one(StatusBar).set_provider_status("Connecting")
        self.query_one(StatusBar).set_request_status("Connecting to Chrome")
        self.query_one(PromptComposer).set_active(False)
        self.runtime.start()

    def post_runtime_event(self, event: RuntimeEventType) -> None:
        """Thread-safe event entry point; no widget is touched by the runtime."""
        self.post_message(RuntimeEventMessage(event))

    async def on_runtime_event_message(self, message: RuntimeEventMessage) -> None:
        await self._handle_runtime_event(message.event)

    async def _handle_runtime_event(self, event: RuntimeEventType) -> None:
        status_bar = self.query_one(StatusBar)

        if isinstance(event, RuntimeReady):
            self._provider = event.provider
            self.query_one("#connection-label", Static).update(event.connection_label)
            status_bar.set_provider_status(event.provider)
            if event.supported:
                status_bar.set_request_status("Idle")
            else:
                status_bar.set_request_status("Provider detected · not validated")
                self.notify(
                    f"{event.provider} was detected, but this version's browser chat flow is only validated with ChatGPT.",
                    title="Provider detected",
                    severity="warning",
                    timeout=8,
                )
            return

        if isinstance(event, RuntimeConnectionFailed):
            self.query_one("#connection-label", Static).update("PROVIDER UNKNOWN  ·  BROWSER NOT CONNECTED  ·  CTRL+R TO RETRY")
            status_bar.set_provider_status("Disconnected")
            status_bar.set_request_status("Browser disconnected")
            self.notify(
                event.message or "Could not connect to the configured Chrome CDP session. Start the RelayPilot Chrome session and press Ctrl+R.",
                title="Browser connection failed",
                severity="warning",
                timeout=8,
            )
            return

        if isinstance(event, RuntimeStopped):
            self.query_one("#connection-label", Static).update("BROWSER DISCONNECTED")
            return

        if isinstance(event, RequestStateChanged):
            label = event.status.value.replace("_", " ").title()
            status_bar.set_request_status(label)
            if event.request_id:
                self._request_started_at.setdefault(event.request_id, monotonic())
                self._set_request_progress(event.request_id, self._progress_label(event.status.value, event.message))
            if event.status.value == "CANCELLING":
                self.query_one(PromptComposer).set_active(True)
            return

        if isinstance(event, EditProposalStarted):
            # Browser-side identity is authoritative, even if the UI receives
            # /edit enablement and response events very close together.
            if event.request_id:
                self._edit_request_ids.add(event.request_id)
                self._set_request_progress(event.request_id, "● Preparing edit proposal…")
            return

        if isinstance(event, EditModeChanged):
            self._edit_mode = event.enabled
            status_bar.set_request_status("Edit mode" if event.enabled else "Idle")
            return

        if isinstance(event, EditReviewResult):
            if not event.successful and event.request_id:
                self._edit_review_failed_requests.add(event.request_id)

            review_ids = self._review_request_ids.get(event.request_id or "", [])
            if review_ids:
                # Preserve the chronological review: the file, reason, and diff
                # remain in conversation, with the final writer result below.
                statuses = self._parse_edit_results(event.message)
                for approval_id in review_ids:
                    review = self._review_widgets.get(approval_id)
                    if review is None:
                        continue
                    results = statuses.get(review.file_name, [])
                    if results:
                        review.mark_result(results.pop(0))
                    elif not event.successful:
                        review.mark_result("Review failed — no further changes approved.")
            else:
                # A malformed proposal has no diff to show. Keep the parser
                # error visible in the conversation without exposing raw tags.
                message = await self._ensure_response_widget(event.request_id)
                if message is not None:
                    await message.replace_text(event.message)

            status_bar.set_request_status(
                "Edit review finished" if event.successful else "Edit proposal invalid"
            )
            self._set_request_progress(
                event.request_id,
                "● Review complete" if event.successful else "● Invalid edit proposal",
            )
            return

        if isinstance(event, ResponseStarted):
            self._set_request_progress(
                event.request_id,
                "● Generating edit proposal…"
                if event.request_id in self._edit_request_ids
                else "● Generating answer…",
            )
            # Structured editing is displayed as a validated diff, not an
            # assistant text bubble containing <AI_EDIT> or a placeholder.
            if event.request_id not in self._edit_request_ids:
                await self._ensure_response_widget(event.request_id)
            return

        if isinstance(event, ResponseDelta):
            if event.request_id in self._edit_request_ids:
                # Raw <AI_EDIT> payload belongs to the validator, not the chat
                # renderer. Keep it in ResponseStore, but don't display tags.
                return
            conversation = self.query_one(ConversationView)
            follow_output = conversation.is_vertical_scroll_end
            message = await self._ensure_response_widget(event.request_id)
            if message is not None:
                message.append_text(event.text)
                if follow_output:
                    conversation.scroll_end(animate=False)
            return

        if isinstance(event, ResponseRevised):
            if event.request_id in self._edit_request_ids:
                return
            conversation = self.query_one(ConversationView)
            follow_output = conversation.is_vertical_scroll_end
            message = await self._ensure_response_widget(event.request_id)
            if message is not None:
                await message.replace_text(event.full_text)
                if follow_output:
                    conversation.scroll_end(animate=False)
            return

        if isinstance(event, RequestCompleted):
            if self._pending_review_request == event.request_id:
                self._abandon_pending_review(event.request_id, "Review ended — not applied.")
            message = self._response_widgets.get(event.request_id or "")
            if message is not None:
                await message.finalize_markdown()
            elapsed = monotonic() - self._request_started_at.get(event.request_id or "", monotonic())
            if event.request_id in self._edit_review_failed_requests:
                status_bar.set_request_status("Edit proposal invalid · no change approved")
            else:
                status_bar.set_request_status(f"Completed · {elapsed:.1f}s")
            self._complete_request_progress(
                event.request_id,
                "● Edit proposal invalid" if event.request_id in self._edit_review_failed_requests else "✓ Completed",
            )
            self._finish_request(event.request_id)
            return

        if isinstance(event, RequestCancelled):
            self._abandon_pending_review(event.request_id, "Cancelled — not applied.")
            message = self._response_widgets.get(event.request_id or "")
            if message is not None:
                await message.mark_interrupted()
            else:
                self.notify("Request cancelled before an answer began.", severity="information", timeout=4)
            status_bar.set_request_status("Cancelled · partial response kept")
            self._complete_request_progress(event.request_id, "● Stopped")
            self._finish_request(event.request_id)
            return

        if isinstance(event, RequestFailed):
            self._abandon_pending_review(event.request_id, "Request failed — not applied.")
            message = self._response_widgets.get(event.request_id or "")
            if message is not None:
                if event.request_id in self._edit_request_ids:
                    await message.replace_text(
                        "Edit request interrupted. The proposal was not applied. "
                        + (event.message or "Check the browser before retrying.")
                    )
                await message.finalize_markdown()
                if event.submission_uncertain:
                    await message.mark_interrupted("Outcome uncertain · check the browser before retrying")
                else:
                    await message.mark_interrupted("Request failed · partial response kept")
            action_required_codes = {
                "AUTH_REQUIRED", "RATE_LIMITED", "USER_ACTION_REQUIRED",
                "NOT_READY", "PROVIDER_NOT_SUPPORTED",
            }
            status_bar.set_request_status(
                "Needs browser action"
                if event.error_code in action_required_codes
                else "Request failed"
            )
            self.notify(
                event.message,
                title=event.error_code.replace("_", " ").title(),
                severity="error",
                timeout=8,
            )
            self._complete_request_progress(event.request_id, "● Failed")
            self._finish_request(event.request_id)
            return

        if isinstance(event, UserActionRequired):
            # RequestFailed immediately follows with the user-facing message.
            # Keep this event for structured action/status handling without
            # producing a duplicate toast.
            status_bar.set_request_status("Needs browser action")
            return

        if isinstance(event, MemoryStatusChanged):
            status_bar.set_memory_status(event.mode)
            return

        if isinstance(event, WorkspaceStatusChanged):
            count = len(event.directories)
            status_bar.set_workspace_status(f"{count} entr{'y' if count == 1 else 'ies'}")
            return

        if isinstance(event, AttachmentStatusChanged):
            self.notify(
                f"{event.file_name}: {event.status}"
                + (f" · {event.message}" if event.message else ""),
                title="Attachment",
                severity="information" if event.status == "ATTACHED" else "warning",
                timeout=5,
            )
            return

        if isinstance(event, EditApprovalRequired):
            # The old CLI displayed each proposed edit sequentially in the
            # transcript. Keep that readable flow and provide persistent
            # Apply/Reject controls, rather than opening a disconnected modal.
            self._edit_request_ids.add(event.request_id or "")
            self._set_request_progress(event.request_id, "● Waiting for edit approval…")
            review = await self.query_one(ConversationView).add_edit_review(
                event.approval_id, event.file_name, event.reason, event.diff
            )
            self._review_widgets[event.approval_id] = review
            self._review_request_ids.setdefault(event.request_id or "", []).append(event.approval_id)
            self._pending_review_id = event.approval_id
            self._pending_review_request = event.request_id
            self.query_one(EditReviewActions).open_for(event.file_name)
            return

        if isinstance(event, UserInputRequested):
            self._set_request_progress(event.request_id, "● Waiting for your input…")
            self.push_screen(
                UserInputScreen(event.prompt),
                lambda value: self.runtime.resolve_user_input(event.input_id, value or "0"),
            )
            return

        if isinstance(event, HelpRequested):
            self.push_screen(HelpScreen(help_text=event.text))
            return

        if isinstance(event, AppNotice):
            severity = event.severity if event.severity in {"information", "warning", "error"} else "information"
            self.notify(event.message, title=event.title, severity=severity, timeout=7)
            return

        if isinstance(event, CommandCompleted):
            status_bar.set_request_status("Idle")
            self._complete_request_progress(event.request_id, "✓ Done")
            self._finish_request(event.request_id)
            return

        if isinstance(event, CommandFailed):
            status_bar.set_request_status("Command failed")
            self._complete_request_progress(event.request_id, "● Command failed")
            self.notify(event.message, title=event.error_code, severity="error", timeout=7)
            self._finish_request(event.request_id)

    @staticmethod
    def _parse_edit_results(message: str) -> dict[str, list[str]]:
        """Read the existing EditWorkflow file/status lines without altering them."""
        results: dict[str, list[str]] = {}
        for line in message.splitlines():
            file_name, separator, status = line.rpartition(": ")
            if separator and status.strip().upper() in {"APPLIED", "REJECTED"}:
                results.setdefault(file_name, []).append(status.strip().upper())
        return results

    def _resolve_pending_review(
        self, approved: bool, *, expected_id: str | None = None
    ) -> None:
        approval_id = self._pending_review_id
        # Old inline controls may still receive a click after the next edit is
        # shown. Never let a button for an old edit approve a newer proposal.
        if expected_id is not None and approval_id != expected_id:
            self.notify("This edit is no longer awaiting approval.", severity="warning")
            return
        if approval_id is None:
            return
        # Prevent double-clicks from approving a proposal twice.
        if not self.runtime.resolve_edit_approval(approval_id, approved):
            self.notify("That edit review is no longer pending.", severity="warning")
            return
        review = self._review_widgets.get(approval_id)
        if review:
            review.mark_decision(approved)
        self._pending_review_id = None
        self._pending_review_request = None
        self.query_one(EditReviewActions).close()
        self.query_one(StatusBar).set_request_status(
            "Applying approved edit…" if approved else "Edit rejected"
        )

    def _abandon_pending_review(self, request_id: str | None, label: str) -> None:
        if not request_id or self._pending_review_request != request_id:
            return
        approval_id = self._pending_review_id
        if approval_id:
            review = self._review_widgets.get(approval_id)
            if review:
                review.mark_result(label)
            self.runtime.resolve_edit_approval(approval_id, False)
        self._pending_review_id = None
        self._pending_review_request = None
        self.query_one(EditReviewActions).close()

    @staticmethod
    def _progress_label(status: str, message: str | None = None) -> str:
        labels = {
            "QUEUED": "● Queued…",
            "PREPARING": "● Preparing request…",
            "SUBMITTING": "● Submitting to browser…",
            "SUBMITTED": "● Submitted…",
            "WAITING_FOR_RESPONSE": "● Waiting for answer…",
            "STREAMING": "● Generating answer…",
            "CANCELLING": "● Stopping…",
            "NEEDS_USER_ACTION": "● Waiting for your input…",
        }
        return labels.get(status, f"● {status.replace('_', ' ').title()}…")

    def _set_request_progress(self, request_id: str | None, label: str) -> None:
        if request_id:
            user_message = self._user_request_widgets.get(request_id)
            if user_message and user_message.is_mounted:
                user_message.set_request_progress(label)

    def _complete_request_progress(self, request_id: str | None, label: str) -> None:
        if not request_id:
            return
        self._set_request_progress(request_id, label)

        def clear() -> None:
            widget = self._user_request_widgets.pop(request_id, None)
            if widget is not None and widget.is_mounted:
                widget.set_request_progress(None)

        self.set_timer(1.8, clear)

    async def _ensure_response_widget(self, request_id: str | None) -> ConversationMessage | None:
        if not request_id:
            return None
        message = self._response_widgets.get(request_id)
        if message is None:
            initial_text = (
                "Preparing edit proposal… Diff review will open after validation."
                if request_id in self._edit_request_ids else ""
            )
            message = await self.query_one(ConversationView).add_message(
                "assistant", initial_text, message_id=request_id
            )
            self._response_widgets[request_id] = message
        return message

    def action_submit_prompt(self) -> None:
        """Queue one prompt/command; the runtime performs all blocking work."""
        if self._active_request_id is not None:
            self.notify("A request is already active. Wait or press Escape to stop it.", severity="warning")
            return

        composer = self.query_one(PromptInput)
        prompt = composer.text.strip()
        if not prompt:
            status = self.query_one(StatusBar)
            status.set_request_status("Enter a prompt first")
            composer.focus()
            return

        # Slash commands remain usable even when VS Code consumes global keys.
        if prompt.casefold() in {"/exit", "/quit"}:
            self.run_worker(self.action_request_exit(), name="relaypilot-exit", exit_on_error=False)
            return
        if prompt.casefold() in {"/help", "/?"}:
            composer.clear()
            self.action_show_help()
            return

        request_id = new_request_id()
        if self._edit_mode and not prompt.startswith("/"):
            self._edit_request_ids.add(request_id)
        if prompt.startswith("/"):
            command = ExecuteCommand(command_text=prompt, request_id=request_id)
        else:
            command = SubmitPrompt(request_id=request_id, prompt=prompt)

        try:
            # Register cancellation synchronously so Escape works even before the
            # async UI worker has mounted the user bubble and queued the command.
            self.runtime.reserve_request(request_id)
        except Exception as error:
            self.notify(str(error), title="Could not submit", severity="error", timeout=6)
            return

        self._active_request_id = request_id
        self._request_started_at[request_id] = monotonic()
        composer.clear()
        self.query_one(PromptComposer).set_active(True)
        self.query_one(StatusBar).set_request_status("Queued")
        self.run_worker(
            self._mount_user_message_then_submit(command, prompt),
            name=f"relaypilot-submit-{request_id}",
            exit_on_error=False,
        )

    async def _mount_user_message_then_submit(self, command, prompt: str) -> None:
        message = None
        try:
            message = await self.query_one(ConversationView).add_message(
                "user", prompt, message_id=command.request_id
            )
            self._user_request_widgets[command.request_id] = message
            message.set_request_progress("● Queued…")
            self.runtime.submit(command)
        except Exception as error:
            self._user_request_widgets.pop(command.request_id, None)
            self._edit_request_ids.discard(command.request_id)
            self.runtime.release_reserved_request(command.request_id)
            if message is not None:
                await message.remove()
            composer = self.query_one(PromptInput)
            composer.load_text(prompt)
            composer.focus()
            self.notify(str(error), title="Could not submit", severity="error", timeout=6)
            self._finish_request(command.request_id)

    def action_cancel_request(self) -> None:
        """Give Escape context-aware behavior for overlays and active requests."""
        screen = self.screen

        # Escape dismisses navigation/help overlays without accidentally stopping
        # a request that may be running behind them.
        if isinstance(screen, (CommandPaletteScreen, HelpScreen)):
            screen.dismiss(None)
            return

        request_id = self._active_request_id
        if request_id is None:
            return

        accepted = self.runtime.cancel_request(CancelRequest(request_id=request_id))
        if accepted:
            self.query_one(StatusBar).set_request_status("Stopping…")
            self.query_one(PromptComposer).set_active(True)

            # Approval/recovery overlays are waiting on a runtime callback.
            # Dismiss them as part of cancelling so their callback releases the
            # runtime's pending interaction immediately.
            if self._pending_review_request == request_id:
                self._abandon_pending_review(request_id, "Cancelled — not applied.")
            if isinstance(screen, UserInputScreen):
                screen.dismiss("0")
        else:
            self.notify("The active request is already finishing or cannot be cancelled.", severity="information", timeout=4)

    def action_open_command_palette(self) -> None:
        self.push_screen(CommandPaletteScreen(), self._on_palette_selection)

    def _on_palette_selection(self, usage: str | None) -> None:
        if not usage:
            return
        composer = self.query_one(PromptInput)
        composer.load_text(usage)
        composer.focus()

    def action_reconnect_browser(self) -> None:
        if self._active_request_id is not None:
            self.notify("Wait for the current request to finish before reconnecting.", severity="warning")
            return
        try:
            self.runtime.reconnect()
            self.query_one(StatusBar).set_request_status("Reconnecting…")
        except Exception as error:
            self.notify(str(error), title="Reconnect failed", severity="error")

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("apply-review-"):
            self._resolve_pending_review(True, expected_id=button_id[len("apply-review-"):])
        elif button_id.startswith("reject-review-"):
            self._resolve_pending_review(False, expected_id=button_id[len("reject-review-"):])
        elif button_id == "apply-edit-button":
            self._resolve_pending_review(True)
        elif event.button.id == "reject-edit-button":
            self._resolve_pending_review(False)
        elif event.button.id == "stop-button":
            self.action_cancel_request()
        elif event.button.id == "newline-button":
            composer = self.query_one(PromptInput)
            composer.insert("\n")
            composer.focus()
        elif event.button.id == "commands-button":
            self.action_open_command_palette()
        elif event.button.id == "help-button":
            self.action_show_help()
        elif event.button.id == "quit-button":
            self.run_worker(self.action_request_exit(), name="relaypilot-exit", exit_on_error=False)

    async def action_request_exit(self) -> None:
        if self._active_request_id is not None:
            self.notify("Stop the active request with Escape before exiting.", severity="warning")
            return
        if self._runtime_shutdown_started:
            return
        self._runtime_shutdown_started = True
        self.query_one(StatusBar).set_request_status("Shutting down…")
        self.runtime.request_shutdown()
        stopped = await asyncio.to_thread(self.runtime.join, 5.0)
        if not stopped:
            self._runtime_shutdown_started = False
            self.query_one(StatusBar).set_request_status("Shutdown pending · waiting for browser operation")
            self.notify(
                "The browser operation has not returned yet. RelayPilot will not force-kill the Playwright thread; press Ctrl+Q again after it settles.",
                title="Shutdown pending",
                severity="warning",
                timeout=8,
            )
            return
        self.exit()

    def _finish_request(self, request_id: str | None) -> None:
        if request_id is None:
            return
        if self._pending_review_request == request_id:
            self._abandon_pending_review(request_id, "Review ended — not applied.")
        self._edit_request_ids.discard(request_id)
        self._edit_review_failed_requests.discard(request_id)
        self._request_started_at.pop(request_id, None)
        if self._active_request_id == request_id:
            self._active_request_id = None
            self.query_one(PromptComposer).set_active(False)
            self.query_one(PromptInput).focus()

    def on_unmount(self) -> None:
        if not self._runtime_shutdown_started:
            self.runtime.request_shutdown(reason="ui_unmounted")
