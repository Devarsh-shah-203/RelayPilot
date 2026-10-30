"""Dedicated owner thread for RelayPilot's synchronous Playwright runtime.

The Textual event loop never touches Playwright objects. All synchronous
Playwright creation, use, and teardown happens inside this worker thread.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from playwright.sync_api import sync_playwright

from application.contracts import (
    AppNotice,
    AttachmentStatusChanged,
    CancelRequest,
    CommandCompleted,
    CommandFailed,
    EditApprovalRequired,
    ExecuteCommand,
    MemoryStatusChanged,
    ReconnectBrowser,
    RequestCancelled,
    RequestCompleted,
    RequestFailed,
    RequestStateChanged,
    RequestStatus,
    ResponseDelta,
    ResponseRevised,
    ResponseStarted,
    RuntimeCommand,
    RuntimeConnectionFailed,
    RuntimeEventType,
    RuntimeReady,
    RuntimeStopped,
    ShutdownRuntime,
    SubmitPrompt,
    UserActionRequired,
    UserInputRequested,
    WorkspaceStatusChanged,
)
from application.gateway_service import GatewayService, ServiceResult
from application.request_state import InvalidRequestTransition, RequestStateMachine
from application.response_store import ResponseSequenceGap, ResponseStore
from browser.chat import BrowserChat
from browser.connection import connect_to_chrome
from browser.provider_identity import provider_name_from_url

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _PendingInteraction:
    event: threading.Event
    value: str | bool | None = None


class GatewayRuntime:
    """Serialize gateway workflows on one thread and publish typed events."""

    def __init__(
        self,
        event_sink: Callable[[RuntimeEventType], None],
        *,
        queue_capacity: int = 64,
        poll_interval: float = 0.1,
    ) -> None:
        self._event_sink = event_sink
        self._commands: queue.Queue[RuntimeCommand] = queue.Queue(maxsize=queue_capacity)
        self._poll_interval = max(0.02, poll_interval)
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._shutdown = threading.Event()
        self._lock = threading.RLock()
        self._active_request_id: str | None = None
        self._cancel_events: dict[str, threading.Event] = {}
        self._pending_user_inputs: dict[str, _PendingInteraction] = {}
        self._pending_approvals: dict[str, _PendingInteraction] = {}
        self._state_machine = RequestStateMachine()
        self._response_store = ResponseStore()
        self._service: GatewayService | None = None
        self._playwright = None
        self._browser = None
        self._provider = "Unknown provider"
        self._provider_supported = False
        self._runtime_error: str | None = None
        self._shutdown_reason = "shutdown"

    @property
    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def provider(self) -> str:
        with self._lock:
            return self._provider

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._shutdown.clear()
            self._started.clear()
            self._thread = threading.Thread(
                target=self._thread_main,
                name="relaypilot-gateway-runtime",
                daemon=True,
            )
            self._thread.start()

    def reserve_request(self, request_id: str) -> None:
        """Register cancellation before the UI begins asynchronously mounting a message.

        This closes the small but important window where Escape could be pressed
        after the user submits but before the runtime command reaches its queue.
        """
        if not request_id or not request_id.strip():
            raise ValueError("request_id cannot be empty")
        if self._shutdown.is_set():
            raise RuntimeError("Gateway runtime is shutting down.")
        with self._lock:
            if request_id in self._cancel_events:
                raise RuntimeError(f"Request is already registered: {request_id}")
            self._cancel_events[request_id] = threading.Event()

    def release_reserved_request(self, request_id: str) -> None:
        """Release a UI reservation when the message could not be mounted/queued."""
        with self._lock:
            if self._active_request_id != request_id:
                self._cancel_events.pop(request_id, None)

    def submit(self, command: RuntimeCommand) -> None:
        """Enqueue a UI command without blocking the Textual thread."""
        if self._shutdown.is_set():
            raise RuntimeError("Gateway runtime is shutting down.")
        if not self.is_alive:
            raise RuntimeError("Gateway runtime is not running. Press Ctrl+R to reconnect.")
        if isinstance(command, (SubmitPrompt, ExecuteCommand)):
            with self._lock:
                self._cancel_events.setdefault(command.request_id, threading.Event())
        try:
            self._commands.put_nowait(command)
        except queue.Full as error:
            if isinstance(command, (SubmitPrompt, ExecuteCommand)):
                with self._lock:
                    self._cancel_events.pop(command.request_id, None)
            raise RuntimeError("Gateway command queue is full.") from error

    def submit_prompt(self, command: SubmitPrompt) -> None:
        self.submit(command)

    def execute_command(self, command: ExecuteCommand) -> None:
        self.submit(command)

    def reconnect(self) -> None:
        """Reconnect in-place, restarting the owner thread if it previously died."""
        if self._shutdown.is_set():
            raise RuntimeError("Gateway runtime is shutting down and cannot reconnect.")
        if not self.is_alive:
            self.start()
            return
        self.submit(ReconnectBrowser())

    def cancel_request(self, command: CancelRequest) -> bool:
        """Signal cancellation out-of-band, even while Playwright is blocking."""
        with self._lock:
            cancel_event = self._cancel_events.get(command.request_id)
            if cancel_event is None or cancel_event.is_set():
                return False
            cancel_event.set()
            try:
                snapshot = self._state_machine.get(command.request_id)
                if snapshot.status in {
                    RequestStatus.PREPARING,
                    RequestStatus.SUBMITTING,
                    RequestStatus.SUBMITTED,
                    RequestStatus.WAITING_FOR_RESPONSE,
                    RequestStatus.STREAMING,
                    RequestStatus.NEEDS_USER_ACTION,
                }:
                    self._transition(
                        command.request_id,
                        RequestStatus.CANCELLING,
                        message="Cancellation requested by user",
                    )
            except (KeyError, InvalidRequestTransition):
                # The request may still be queued. The event is already set and
                # the worker will observe it before performing browser actions.
                pass
            return True

    def resolve_user_input(self, input_id: str, value: str) -> bool:
        with self._lock:
            pending = self._pending_user_inputs.get(input_id)
            if pending is None:
                return False
            pending.value = value
            pending.event.set()
            return True

    def resolve_edit_approval(self, approval_id: str, approved: bool) -> bool:
        with self._lock:
            pending = self._pending_approvals.get(approval_id)
            if pending is None:
                return False
            pending.value = bool(approved)
            pending.event.set()
            return True

    def request_shutdown(self, reason: str = "application_exit") -> None:
        """Ask the active operation to stop, then let the owner thread clean up."""
        if self._shutdown.is_set():
            return
        self._shutdown_reason = reason
        self._shutdown.set()
        with self._lock:
            for cancel_event in self._cancel_events.values():
                cancel_event.set()
            for pending in self._pending_user_inputs.values():
                pending.value = "0"
                pending.event.set()
            for pending in self._pending_approvals.values():
                pending.value = False
                pending.event.set()
        try:
            self._commands.put_nowait(ShutdownRuntime(reason=reason))
        except queue.Full:
            # The worker will exit after its current operation observes shutdown.
            pass

    def join(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _emit(self, event: RuntimeEventType) -> None:
        try:
            self._event_sink(event)
        except Exception:
            logger.exception("Could not publish RelayPilot runtime event: %r", event)

    def _thread_main(self) -> None:
        reason = "shutdown"
        try:
            with sync_playwright() as playwright:
                self._playwright = playwright
                self._service = GatewayService(
                    None,
                    emit_event=self._emit,
                    request_user_input=self._request_user_input,
                    request_edit_approval=self._request_edit_approval,
                )
                self._connect_browser()
                self._started.set()

                while not self._shutdown.is_set():
                    try:
                        command = self._commands.get(timeout=self._poll_interval)
                    except queue.Empty:
                        continue
                    if isinstance(command, ShutdownRuntime):
                        reason = command.reason
                        break
                    if isinstance(command, ReconnectBrowser):
                        self._connect_browser()
                        continue
                    if isinstance(command, (SubmitPrompt, ExecuteCommand)):
                        self._process_request(command)
        except Exception as error:
            self._runtime_error = str(error)
            logger.exception("RelayPilot runtime terminated unexpectedly")
            self._emit(RuntimeConnectionFailed(message=f"Runtime stopped unexpectedly: {error}"))
        finally:
            # Do not call browser.close(): CDP attached to a user-owned Chrome
            # process, which RelayPilot must not terminate.
            self._service = None
            self._browser = None
            self._playwright = None
            with self._lock:
                for pending in self._pending_user_inputs.values():
                    pending.value = "0"
                    pending.event.set()
                for pending in self._pending_approvals.values():
                    pending.value = False
                    pending.event.set()
            self._emit(RuntimeStopped(reason=self._shutdown_reason or reason))

    def _connect_browser(self) -> None:
        if self._playwright is None:
            self._emit(RuntimeConnectionFailed(message="Playwright runtime is not initialized."))
            return
        try:
            browser = connect_to_chrome(self._playwright)
            chat = BrowserChat(browser)
            provider = provider_name_from_url(chat.page.url)
            supported = provider == "ChatGPT"
            self._browser = browser
            self._provider = provider
            self._provider_supported = supported
            if self._service is None:
                raise RuntimeError("Application services were not initialized.")
            # The current repository's chat automation is validated with ChatGPT.
            # Other providers are identified in the UI but not sent prompts yet.
            self._service.replace_chat(chat if supported else None)
            connection_label = (
                f"PROVIDER: {provider.upper()}  ·  CONNECTED"
                if supported
                else f"PROVIDER: {provider.upper()}  ·  DETECTED / NOT VALIDATED"
            )
            self._emit(
                RuntimeReady(
                    provider=provider,
                    connection_label=connection_label,
                    supported=supported,
                )
            )
        except Exception as error:
            # A reconnect failure must not leave an old page/browser object
            # eligible for a later prompt. Never close the user's Chrome process.
            self._runtime_error = str(error)
            self._browser = None
            self._provider = "Unknown provider"
            self._provider_supported = False
            if self._service is not None:
                self._service.replace_chat(None)
            logger.warning("Could not connect to Chrome through CDP: %s", error)
            self._emit(RuntimeConnectionFailed(message=str(error)))

    def _process_request(self, command: SubmitPrompt | ExecuteCommand) -> None:
        request_id = command.request_id
        with self._lock:
            cancel_event = self._cancel_events.setdefault(request_id, threading.Event())
            self._active_request_id = request_id

        is_slash_command = isinstance(command, ExecuteCommand)
        state_created = False

        def ensure_state() -> None:
            nonlocal state_created
            if state_created:
                return
            self._state_machine.create(request_id)
            state_created = True
            self._publish_state(request_id, RequestStatus.QUEUED, None, already_created=True)

        def on_state(value: str, message: str | None = None) -> None:
            nonlocal state_created
            try:
                target = RequestStatus(value)
            except ValueError:
                logger.debug("Ignoring unknown request state from browser: %s", value)
                return
            ensure_state()
            self._publish_state(request_id, target, message)

        def on_response_start() -> None:
            ensure_state()
            try:
                self._response_store.start(request_id)
            except ValueError:
                pass
            self._emit(ResponseStarted(request_id=request_id))

        def on_response_change(change) -> None:
            ensure_state()
            try:
                self._response_store.get(request_id)
            except KeyError:
                self._response_store.start(request_id)
                self._emit(ResponseStarted(request_id=request_id))
            try:
                if change.kind == "delta":
                    snapshot = self._response_store.append_delta(
                        request_id, sequence=change.sequence, text=change.text
                    )
                    if snapshot.last_sequence != change.sequence:
                        return  # duplicate or stale event already incorporated
                    self._emit(
                        ResponseDelta(
                            request_id=request_id,
                            sequence=snapshot.last_sequence,
                            text=change.text,
                        )
                    )
                else:
                    snapshot = self._response_store.revise(
                        request_id, sequence=change.sequence, full_text=change.text
                    )
                    if snapshot.last_sequence != change.sequence:
                        return  # duplicate or stale event already incorporated
                    self._emit(
                        ResponseRevised(
                            request_id=request_id,
                            sequence=snapshot.last_sequence,
                            full_text=change.text,
                        )
                    )
            except ResponseSequenceGap:
                logger.exception("Response sequence gap for request %s", request_id)
                # ResponseChange carries the complete source snapshot so a missing
                # delta can be reconciled without mistaking a suffix for the whole.
                self._force_response_revision(request_id, change.snapshot or change.text)

        try:
            if cancel_event.is_set():
                ensure_state()
                self._finish_cancelled(request_id, state_created=True)
                return

            if isinstance(command, SubmitPrompt):
                if not self._chat_available():
                    ensure_state()
                    if self._provider and self._provider != "Unknown provider" and not self._provider_supported:
                        error_code = "PROVIDER_NOT_SUPPORTED"
                        message = (
                            f"{self._provider} was detected, but this version's browser chat "
                            "workflow is only validated with ChatGPT. Open ChatGPT in the "
                            "remote-debugging Chrome window, then press Ctrl+R."
                        )
                    else:
                        error_code = "BROWSER_NOT_CONNECTED"
                        message = (
                            "No supported chat page is connected. Start Chrome with "
                            "remote debugging, open ChatGPT, then press Ctrl+R to reconnect."
                        )
                    self._finish_failed(
                        request_id,
                        error_code=error_code,
                        message=message,
                    )
                    return
                assert self._service is not None
                result = self._service.submit_prompt(
                    request_id,
                    command.prompt,
                    cancel_event=cancel_event,
                    on_state=on_state,
                    on_response_start=on_response_start,
                    on_response_change=on_response_change,
                )
            else:
                assert self._service is not None
                result = self._service.execute_command(
                    command.command_text,
                    request_id,
                    cancel_event=cancel_event,
                    on_state=on_state,
                    on_response_start=on_response_start,
                    on_response_change=on_response_change,
                )

            if result.state is None:
                self._emit(
                    CommandCompleted(
                        request_id=request_id,
                        command_id=command.command_id,
                        message=result.message,
                    )
                )
                return

            ensure_state()
            if result.state in {"COMPLETED", "CANCELLED", "CANCEL_UNCONFIRMED"}:
                self._reconcile_final_response(request_id, result.response)
            if result.state == "COMPLETED":
                self._finish_completed(request_id)
            elif result.state == "CANCELLED":
                self._finish_cancelled(request_id, state_created=True)
            elif result.state == "SUBMISSION_UNKNOWN":
                self._finish_submission_unknown(request_id, result.response)
            else:
                self._finish_failed(
                    request_id,
                    error_code=result.state,
                    message=result.response or result.state.replace("_", " ").title(),
                    retryable=result.state in {"NOT_READY", "SEND_ERROR", "TIMEOUT_UNKNOWN"},
                    submission_uncertain=result.state in {"TIMEOUT_UNKNOWN", "CANCEL_UNCONFIRMED"},
                )

        except Exception as error:
            logger.exception("Request %s failed inside GatewayRuntime", request_id)
            if state_created:
                # Browser-backed commands already have a request lifecycle; emit
                # one RequestFailed event rather than a duplicate CommandFailed.
                self._finish_failed(
                    request_id,
                    error_code=type(error).__name__,
                    message=str(error),
                )
            elif is_slash_command:
                # Local slash commands can fail before a browser request state
                # exists, so preserve their dedicated command failure event.
                self._emit(
                    CommandFailed(
                        request_id=request_id,
                        command_id=command.command_id,
                        error_code=type(error).__name__,
                        message=str(error),
                    )
                )
            else:
                self._emit(
                    AppNotice(
                        title="Request error",
                        message=str(error),
                        severity="error",
                        request_id=request_id,
                    )
                )
        finally:
            with self._lock:
                self._active_request_id = None
                self._cancel_events.pop(request_id, None)

    def _chat_available(self) -> bool:
        return bool(
            self._provider_supported
            and self._service is not None
            and self._service.chat is not None
        )

    def _publish_state(
        self,
        request_id: str,
        status: RequestStatus,
        message: str | None,
        *,
        already_created: bool = False,
    ) -> None:
        try:
            if already_created:
                snapshot = self._state_machine.get(request_id)
            else:
                current = self._state_machine.get(request_id)
                if current.status == status:
                    return
                if current.status == RequestStatus.CANCELLING and status not in {
                    RequestStatus.COMPLETED,
                    RequestStatus.CANCELLED,
                    RequestStatus.SUBMISSION_UNKNOWN,
                    RequestStatus.FAILED,
                }:
                    return
                snapshot = self._state_machine.transition(request_id, status, message=message)
            self._emit(
                RequestStateChanged(
                    request_id=request_id,
                    previous_status=None if already_created else current.status,
                    status=snapshot.status,
                    message=message,
                )
            )
        except (KeyError, InvalidRequestTransition):
            logger.debug("Ignoring invalid lifecycle transition %s -> %s", request_id, status.value)

    def _finish_completed(self, request_id: str) -> None:
        self._transition(request_id, RequestStatus.COMPLETED, message="Response completed")
        try:
            self._response_store.mark_completed(request_id)
        except KeyError:
            pass
        self._emit(RequestCompleted(request_id=request_id))

    def _finish_cancelled(self, request_id: str, *, state_created: bool) -> None:
        if state_created:
            self._transition(request_id, RequestStatus.CANCELLED, message="Stopped by user")
        self._emit(RequestCancelled(request_id=request_id, partial_response_preserved=True))

    def _finish_submission_unknown(self, request_id: str, message: str) -> None:
        self._transition(
            request_id,
            RequestStatus.SUBMISSION_UNKNOWN,
            message="Check the browser before retrying; submission may have occurred.",
        )
        self._emit(
            RequestFailed(
                request_id=request_id,
                error_code="SUBMISSION_UNKNOWN",
                message=message or "Submission outcome is uncertain. Check the browser before retrying.",
                retryable=False,
                submission_uncertain=True,
            )
        )

    def _finish_failed(
        self,
        request_id: str,
        *,
        error_code: str,
        message: str,
        retryable: bool = False,
        submission_uncertain: bool = False,
    ) -> None:
        self._transition(request_id, RequestStatus.FAILED, message=message)
        if error_code in {"AUTH_REQUIRED", "RATE_LIMITED", "USER_ACTION_REQUIRED", "NOT_READY", "PROVIDER_NOT_SUPPORTED"}:
            action = {
                "AUTH_REQUIRED": "sign_in",
                "RATE_LIMITED": "wait_or_change_model",
                "USER_ACTION_REQUIRED": "complete_browser_action",
                "NOT_READY": "open_chat_page",
                "PROVIDER_NOT_SUPPORTED": "select_supported_provider",
            }.get(error_code, "check_browser")
            self._emit(
                UserActionRequired(
                    request_id=request_id,
                    action=action,
                    message=message,
                )
            )
        self._emit(
            RequestFailed(
                request_id=request_id,
                error_code=error_code,
                message=message,
                retryable=retryable,
                submission_uncertain=submission_uncertain,
            )
        )

    def _transition(self, request_id: str, status: RequestStatus, *, message: str | None = None) -> None:
        self._publish_state(request_id, status, message)

    def _force_response_revision(self, request_id: str, full_text: str) -> None:
        try:
            existing = self._response_store.get(request_id)
            sequence = existing.last_sequence + 1
            snapshot = self._response_store.revise(
                request_id, sequence=sequence, full_text=full_text
            )
            self._emit(
                ResponseRevised(
                    request_id=request_id,
                    sequence=snapshot.last_sequence,
                    full_text=full_text,
                    reason="sequence_reconciliation",
                )
            )
        except Exception:
            logger.exception("Could not reconcile response stream for %s", request_id)

    def _reconcile_final_response(self, request_id: str, final_text: str) -> None:
        if not final_text:
            return
        try:
            current = self._response_store.get(request_id)
        except KeyError:
            self._response_store.start(request_id)
            self._emit(ResponseStarted(request_id=request_id))
            self._response_store.append_delta(request_id, sequence=1, text=final_text)
            self._emit(ResponseDelta(request_id=request_id, sequence=1, text=final_text))
            return
        if final_text == current.raw_content:
            return
        if final_text.startswith(current.raw_content):
            delta = final_text[len(current.raw_content):]
            if delta:
                sequence = current.last_sequence + 1
                snapshot = self._response_store.append_delta(
                    request_id, sequence=sequence, text=delta
                )
                self._emit(
                    ResponseDelta(
                        request_id=request_id,
                        sequence=snapshot.last_sequence,
                        text=delta,
                    )
                )
            return
        self._force_response_revision(request_id, final_text)

    def _pause_request_for_interaction(self, request_id: str | None, message: str) -> bool:
        """Expose an upload/edit approval wait as NEEDS_USER_ACTION in the UI."""
        if not request_id:
            return False
        try:
            previous = self._state_machine.get(request_id).status
            snapshot = self._state_machine.pause_for_user_action(
                request_id, message=message
            )
            self._emit(
                RequestStateChanged(
                    request_id=request_id,
                    previous_status=previous,
                    status=snapshot.status,
                    message=message,
                )
            )
            return True
        except (KeyError, InvalidRequestTransition):
            return False

    def _resume_request_after_interaction(self, request_id: str | None) -> None:
        """Restore the request state after a modal interaction has completed."""
        if not request_id:
            return
        try:
            snapshot = self._state_machine.get(request_id)
            previous = snapshot.status
            resumed = self._state_machine.resume_after_user_action(request_id)
            self._emit(
                RequestStateChanged(
                    request_id=request_id,
                    previous_status=previous,
                    status=resumed.status,
                    message=None,
                )
            )
        except (KeyError, InvalidRequestTransition):
            # Cancellation may have moved NEEDS_USER_ACTION -> CANCELLING while
            # the dialog was open. In that case the terminal state wins.
            return

    def _request_user_input(self, prompt: str) -> str:
        with self._lock:
            active_id = self._active_request_id
            active_cancel = self._cancel_events.get(active_id or "")
        if self._shutdown.is_set() or (active_cancel and active_cancel.is_set()):
            return "0"

        input_id = f"input_{uuid4().hex}"
        pending = _PendingInteraction(event=threading.Event())
        with self._lock:
            self._pending_user_inputs[input_id] = pending
            request_id = self._active_request_id
            cancel_event = self._cancel_events.get(request_id or "")
        paused = self._pause_request_for_interaction(
            request_id, "Waiting for attachment recovery input"
        )
        self._emit(
            UserInputRequested(
                request_id=request_id,
                input_id=input_id,
                prompt=prompt,
            )
        )
        try:
            while not pending.event.wait(self._poll_interval):
                if self._shutdown.is_set() or (cancel_event and cancel_event.is_set()):
                    return "0"
            if self._shutdown.is_set() or (cancel_event and cancel_event.is_set()):
                return "0"
            return str(pending.value if pending.value is not None else "0")
        finally:
            with self._lock:
                self._pending_user_inputs.pop(input_id, None)
            if paused and not self._shutdown.is_set() and not (cancel_event and cancel_event.is_set()):
                self._resume_request_after_interaction(request_id)

    def _request_edit_approval(self, file_name: str, reason: str, diff: str) -> bool:
        with self._lock:
            active_id = self._active_request_id
            active_cancel = self._cancel_events.get(active_id or "")
        if self._shutdown.is_set() or (active_cancel and active_cancel.is_set()):
            return False

        approval_id = f"approval_{uuid4().hex}"
        pending = _PendingInteraction(event=threading.Event())
        with self._lock:
            self._pending_approvals[approval_id] = pending
            request_id = self._active_request_id
            cancel_event = self._cancel_events.get(request_id or "")
        paused = self._pause_request_for_interaction(
            request_id, f"Waiting for edit approval: {file_name}"
        )
        self._emit(
            EditApprovalRequired(
                request_id=request_id,
                approval_id=approval_id,
                file_name=file_name,
                reason=reason,
                diff=diff,
            )
        )
        try:
            while not pending.event.wait(self._poll_interval):
                if self._shutdown.is_set() or (cancel_event and cancel_event.is_set()):
                    return False
            if self._shutdown.is_set() or (cancel_event and cancel_event.is_set()):
                return False
            return bool(pending.value)
        finally:
            with self._lock:
                self._pending_approvals.pop(approval_id, None)
            if paused and not self._shutdown.is_set() and not (cancel_event and cancel_event.is_set()):
                self._resume_request_after_interaction(request_id)

