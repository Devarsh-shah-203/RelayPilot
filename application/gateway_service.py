"""Application services for RelayPilot's browser-backed terminal client.

All instances are created and used by GatewayRuntime on its owning runtime
thread. This module deliberately has no Textual or Playwright imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from tempfile import TemporaryDirectory
from pathlib import Path
from threading import Event
from typing import Callable

from attachments.integration import (
    build_delivery_prompt,
    build_inline_context,
    inline_fallback_base_characters,
    plan_referenced_files,
    upload_and_confirm,
)
from context.builder import ContextBuilder
from context.session import MemoryContextSession
from files.context import FileContextBuilder
from files.edit_prompt import build_edit_prompt
from files.edit_workflow import EditWorkflow
from files.structured_context import StructuredContextBuilder
from files.notebook_delivery import NotebookPreparationService, NotebookSelectionCancelled
from attachments.models import AttachmentRecord, UploadReport
from attachments.policy import FileKind, estimate_tokens
from memory.prompts import build_save_prompt
from memory.saver import MemorySaver
from memory.registry import MemoryRegistry
from application.command_registry import render_command_help
from workspace.manager import WorkspaceManager


@dataclass(frozen=True, slots=True)
class ServiceResult:
    """Result of a local command or a browser-backed request."""

    state: str | None = None
    response: str = ""
    submitted: bool = False
    message: str = ""


class GatewayService:
    """Preserve the original gateway workflows behind a UI-independent facade."""

    def __init__(
        self,
        chat,
        *,
        emit_event: Callable[[object], None],
        request_user_input: Callable[[str], str],
        request_edit_approval: Callable[[str, str, str], bool],
        project_dir: str | Path | None = None,
    ) -> None:
        self.chat = chat
        self.emit_event = emit_event
        self.request_user_input = request_user_input
        self.request_edit_approval = request_edit_approval

        # Memory and RelayPilot metadata live inside the installed clone so a
        # shipped copy remains self-contained regardless of its parent folder.
        self.project_root = Path(project_dir).resolve() if project_dir is not None else Path(__file__).resolve().parents[1]
        self.memory_registry = MemoryRegistry(self.project_root)
        self.workspace = WorkspaceManager()
        self.context_builder = ContextBuilder(project_dir=self.project_root)
        # Notebook cell selection is interactive. Route its prompt and summary
        # through the runtime/UI bridge instead of calling input()/print() while
        # Textual owns the terminal.
        self._notebook_output_lines: list[str] = []
        structured_context = StructuredContextBuilder(
            input_fn=self._request_notebook_selection,
            output_fn=self._capture_notebook_output,
        )
        self.file_context_builder = FileContextBuilder(
            self.workspace,
            structured_context_builder=structured_context,
        )
        self.memory_session = MemoryContextSession(self.project_root)
        self.memory_saver = MemorySaver(project_dir=self.project_root)
        self.target_workspace_id: int | None = None
        self.edit_workflow = EditWorkflow(
            workspace_manager=self.workspace,
            approval_function=self.request_edit_approval,
        )
        self.edit_mode = False
        self.edit_protocol_sent = False
        self._publish_memory_status()
        self._publish_workspace_status()

    def replace_chat(self, chat) -> None:
        """Attach a newly connected BrowserChat without resetting local sessions."""
        self.chat = chat

    def _capture_notebook_output(self, *values: object) -> None:
        """Capture notebook context instructions for the next UI input dialog."""
        line = " ".join(str(value) for value in values)
        if "NOTEBOOK CONTEXT SELECTION" in line:
            # A new explicit notebook reference starts a fresh selection summary.
            self._notebook_output_lines.clear()
        self._notebook_output_lines.append(line)

    def _request_notebook_selection(self, prompt: str) -> str:
        """Show notebook cell map and selection prompt in a Textual modal."""
        details = "\n".join(self._notebook_output_lines).strip()
        full_prompt = f"{details}\n\n{prompt}" if details else prompt
        return self.request_user_input(full_prompt)

    def _notice(self, message: str, *, title: str = "RelayPilot", severity: str = "information") -> None:
        from application.contracts import AppNotice

        if message:
            self.emit_event(AppNotice(title=title, message=message, severity=severity))

    def _publish_memory_status(self) -> None:
        from application.contracts import MemoryStatusChanged

        status = self.memory_session.get_status()
        self.emit_event(
            MemoryStatusChanged(mode=status["mode"], snapshot=status["snapshot"])
        )

    def _publish_workspace_status(self) -> None:
        from application.contracts import WorkspaceStatusChanged

        directories = tuple(
            f"[{entry['id']}] {Path(entry['path']).name}"
            for entry in self.workspace.get_status()
        )
        self.emit_event(WorkspaceStatusChanged(directories=directories))

    def submit_prompt(
        self,
        request_id: str,
        prompt: str,
        *,
        cancel_event: Event,
        on_state: Callable[[str, str | None], None],
        on_response_start: Callable[[], None],
        on_response_change: Callable[[object], None],
    ) -> ServiceResult:
        return self._run_prompt(
            request_id,
            prompt,
            cancel_event=cancel_event,
            on_state=on_state,
            on_response_start=on_response_start,
            on_response_change=on_response_change,
        )

    def execute_command(
        self,
        command_text: str,
        request_id: str,
        *,
        cancel_event: Event,
        on_state: Callable[[str, str | None], None],
        on_response_start: Callable[[], None],
        on_response_change: Callable[[object], None],
    ) -> ServiceResult:
        """Route local commands and explicit one-shot model requests."""
        prompt = command_text.strip()
        if prompt and prompt.split(None, 1)[0].casefold() == "memory":
            prompt = "/" + prompt
        command = prompt.casefold()

        if command == "/commands" or command.startswith("/commands "):
            from application.contracts import HelpRequested

            category = prompt[len("/commands"):].strip()
            lines: list[str] = []
            render_command_help(
                category or None,
                output=lambda *parts: lines.append(" ".join(map(str, parts))),
            )
            self.emit_event(HelpRequested(text="\n".join(lines)))
            return ServiceResult(message="Command reference opened.")

        if command == "/edit" or command.startswith("/edit "):
            selected = prompt[len("/edit"):].strip()
            if selected:
                if not selected.isdigit():
                    raise ValueError("Usage: /edit [existing_workspace_id]")
                workspace_id = int(selected)
                entries = self.workspace.list_directories()
                destination = next((entry for entry in entries if entry["id"] == workspace_id), None)
                if destination is None or not Path(destination["path"]).is_dir():
                    raise ValueError("Select an existing registered workspace for file creation.")
                self.target_workspace_id = workspace_id
                self.edit_workflow.target_workspace_id = workspace_id
                self.edit_protocol_sent = False
                self._notice(f"New files may be proposed in workspace [{workspace_id}] {destination['path']}. Every file requires approval.", title="Edit destination")
            if self.edit_mode:
                message = "Already in EDIT MODE."
            else:
                self.edit_mode = True
                self.edit_protocol_sent = False
                message = (
                    "EDIT MODE enabled. Describe the change and reference files with "
                    "<workspace_id/relative/path>. Use /cancel to leave edit mode."
                )
            from application.contracts import EditModeChanged
            self.emit_event(EditModeChanged(enabled=self.edit_mode))
            self._notice(message, title="Edit mode")
            return ServiceResult(message=message)

        if command == "/cancel":
            if self.edit_mode:
                self.edit_mode = False
                self.edit_protocol_sent = False
                self.target_workspace_id = None
                self.edit_workflow.target_workspace_id = None
                message = "EDIT MODE disabled."
            else:
                message = "Not currently in EDIT MODE."
            from application.contracts import EditModeChanged
            self.emit_event(EditModeChanged(enabled=self.edit_mode))
            self._notice(message, title="Edit mode")
            return ServiceResult(message=message)

        if command == "/workspace":
            entries = self.workspace.get_status()
            if entries:
                message = "\n".join(
                    f"[{entry['id']}] {Path(entry['path']).name} — {entry['status']}\n    {entry['path']}"
                    for entry in entries
                )
            else:
                message = "No workspace directories configured. Add one with /workspace add <directory>."
            self._notice(message, title="Workspace")
            self._publish_workspace_status()
            return ServiceResult(message=message)

        if command.startswith("/workspace add"):
            raw_path = prompt[len("/workspace add"):].strip()
            if not raw_path:
                raise ValueError("Usage: /workspace add <directory>")
            path = raw_path.strip('"')
            entry = self.workspace.add_directory(path)
            message = f"Workspace directory added: [{entry['id']}] {entry['path']}"
            self._notice(message, title="Workspace")
            self._publish_workspace_status()
            return ServiceResult(message=message)

        if command.startswith("/workspace remove"):
            raw_id = prompt[len("/workspace remove"):].strip()
            if not raw_id or not raw_id.isdigit():
                raise ValueError("Usage: /workspace remove <id>")
            removed = self.workspace.remove_directory(int(raw_id))
            message = f"Workspace entry removed: [{removed['id']}] {removed['path']}"
            self._notice(message, title="Workspace")
            self._publish_workspace_status()
            return ServiceResult(message=message)

        if command == "/workspace clear":
            count = len(self.workspace.list_directories())
            self.workspace.clear()
            message = f"Workspace cleared. {count} director{'y' if count == 1 else 'ies'} removed."
            self._notice(message, title="Workspace")
            self._publish_workspace_status()
            return ServiceResult(message=message)

        if command == "/memory":
            entries = self.memory_registry.list_entries()
            message = "Registered project memory:\n" + "\n".join(
                f"- {entry['path']}" + (" (disabled)" if not entry['enabled'] else "")
                for entry in entries
            ) if entries else "No registered memory files. Use memory add or memory renew."
            self._notice(message, title="Memory")
            return ServiceResult(message=message)

        if command == "/memory renew":
            result = self.memory_registry.renew()
            lines = [f"Registered {len(result.added)} new Markdown memory file(s)."]
            lines.extend(f"- {name}" for name in result.added)
            lines.extend(f"Warning: {warning}" for warning in result.warnings)
            lines.extend(f"Missing registered memory: {name}" for name in result.missing)
            message = "\n".join(lines)
            self._notice(message, title="Memory")
            return ServiceResult(message=message)

        if command == "/memory add" or command.startswith("/memory add "):
            name = prompt[len("/memory add"):].strip().strip('"')
            if not name:
                name = self.request_user_input(
                    "MEMORY FILENAME\nName the new memory file (for example architecture). "
                    "RelayPilot adds .md automatically."
                ).strip()
            if name == "CANCEL_MEMORY":
                return ServiceResult(message="Memory creation cancelled.")
            content = self.request_user_input("MEMORY INITIAL CONTENT\nOptional Markdown content. Leave blank to create an empty file, then Continue.")
            if content == "CANCEL_MEMORY" or cancel_event.is_set():
                return ServiceResult(message="Memory creation cancelled.")
            created = self.memory_registry.add(name, content)
            message = f"Created and registered .ai/{created.name}."
            self._notice(message, title="Memory")
            return ServiceResult(message=message)

        if command == "/memory refresh" or command.startswith("/memory refresh "):
            self.memory_session.refresh()
            self._publish_memory_status()
            if command == "/memory refresh":
                message = "Project memory is queued for the next model request only, including /edit."
                self._notice(message, title="Memory")
                return ServiceResult(message=message)
            prompt = prompt[len("/memory refresh"):].strip()
            return self._run_prompt(
                request_id, prompt, cancel_event=cancel_event,
                on_state=on_state, on_response_start=on_response_start,
                on_response_change=on_response_change,
            )

        if command.startswith("/memory "):
            raise ValueError("Use memory, memory refresh, memory add, or memory renew. Persistent memory on/use/off commands are retired.")

        if command == "/save":
            return self._run_prompt(
                request_id,
                build_save_prompt(),
                cancel_event=cancel_event,
                on_state=on_state,
                on_response_start=on_response_start,
                on_response_change=on_response_change,
                save_result=True,
            )

        # Preserve the legacy behavior: unrecognized slash commands are treated as
        # ordinary prompt text and sent to the selected browser chat.
        return self._run_prompt(
            request_id, prompt, cancel_event=cancel_event,
            on_state=on_state, on_response_start=on_response_start,
            on_response_change=on_response_change,
        )

    def _target_workspace_context(self):
        workspace_id = getattr(self, "target_workspace_id", None)
        if workspace_id is None:
            return "No CREATE destination has been selected. Only modify explicitly delivered existing files."
        entry = next((item for item in self.workspace.list_directories() if item["id"] == workspace_id), None)
        if entry is None:
            raise ValueError("The selected CREATE workspace was removed; select it again with /edit <id>.")
        root = Path(entry["path"])
        if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()) or not root.is_dir():
            raise ValueError("The selected CREATE workspace is unavailable or redirected.")
        existing = "none" if next(root.iterdir(), None) is None else "present; use only explicitly delivered files as source context"
        return {"id": workspace_id, "path": str(root.resolve()), "existing_files": existing}

    def _run_prompt(
        self,
        request_id: str,
        prompt: str,
        *,
        cancel_event: Event,
        on_state: Callable[[str, str | None], None],
        on_response_start: Callable[[], None],
        on_response_change: Callable[[object], None],
        save_result: bool = False,
    ) -> ServiceResult:
        if not prompt.strip():
            raise ValueError("Prompt cannot be empty.")
        if cancel_event.is_set():
            return ServiceResult(state="CANCELLED")
        if self.chat is None:
            return ServiceResult(
                state="NOT_READY",
                response=(
                    "No supported browser chat is connected. Open ChatGPT in the "
                    "remote-debugging Chrome window, then use Ctrl+R to reconnect."
                ),
                submitted=False,
            )

        on_state("PREPARING", "Preparing prompt and project context")
        session = getattr(self, "memory_session", None)
        include_memory = session is not None and session.should_include_memory()
        memory_documents = self.memory_registry.load_current() if include_memory else []
        memory_by_source = {document.path.resolve(): document for document in memory_documents}
        # Memory uploads do not occupy inline space unless recovery selects them.
        # Reserve the manifest labels in both notebook and fallback calculations.
        memory_characters = 0
        memory_manifest_reserve = sum(500 + 3 * len(doc.relative_path) for doc in memory_documents)
        target_workspace = self._target_workspace_context() if self.edit_mode else None

        attachment_plan = plan_referenced_files(
            prompt,
            self.workspace,
            memory_characters=memory_characters,
            edit_mode=self.edit_mode,
            trusted_memory_paths=tuple(memory_by_source),
        )
        if attachment_plan.preflight_issues:
            self._notice("\n".join(attachment_plan.preflight_issues), title="Workspace files", severity="warning")
        if attachment_plan.excluded_references:
            self._notice(
                "Excluded from selection:\n" + "\n".join(
                    f"- {item.reference}: {item.reason}" for item in attachment_plan.excluded_references
                ), title="Workspace files", severity="warning",
            )
        if attachment_plan.skipped_count:
            self._notice(
                f"Bulk selection skipped {attachment_plan.skipped_count} generated, sensitive, unsupported, binary, or notebook file(s).",
                title="Workspace files",
                severity="warning",
            )

        selected_labels = {}
        for reference in attachment_plan.references:
            selected_labels.setdefault(reference.path.resolve(), f"<{reference.raw_reference}>")
        for source, document in memory_by_source.items():
            label = document.relative_path
            if source in selected_labels:
                label += " (also " + selected_labels[source] + ")"
            selected_labels[source] = label
        if selected_labels:
            self._notice("Selected context files:\n" + "\n".join(
                f"- {label}" for label in selected_labels.values()
            ), title="Workspace files")

        # Notebook selection is the one exception to blanket upload-first:
        # selections fitting the budget are inlined; larger selections upload
        # the original notebook if fully approved, else a filtered copy.
        # Every temporary attachment is deleted after the request completes.
        from application.contracts import EditProposalStarted
        if self.edit_mode:
            self.emit_event(EditProposalStarted(request_id=request_id))

        attached_paths: list[Path] = []
        inline_paths: list[Path] = []
        inline_contexts: list[dict] = []
        upload_report = None
        filename_overrides: dict[Path, str] = {}

        # Reserve room for the structured edit protocol before choosing inline
        # notebook delivery. Without this, a small selection can appear to fit
        # but overflow after EDIT_PROTOCOL is wrapped around the final prompt.
        edit_prompt_reserve = 0
        if self.edit_mode:
            edit_prompt_reserve = max(
                0,
                len(build_edit_prompt(
                    prompt, [], include_instructions=not self.edit_protocol_sent,
                    target_workspace=target_workspace,
                )) - len(prompt),
            )
        notebook_count = sum(
            1 for path in attachment_plan.paths if path.suffix.casefold() == '.ipynb'
        )
        notebook_manifest_reserve = notebook_count * 500

        # Reject a request whose known attachment labels and instructions
        # already cannot fit before spending provider upload capacity. Final
        # serialized fallback content is checked again immediately before send.
        memory_budget_lines = ["PROJECT MEMORY DELIVERY", "======================="]
        for number, document in enumerate(memory_documents, 1):
            staged_name = f"memory-{number:03d}-{document.path.name}"
            memory_budget_lines.append(
                f"- {document.relative_path}: planned attachment: {staged_name}"
            )
        budget_prompt = prompt
        if memory_documents:
            budget_prompt += "\n\n" + "\n".join(memory_budget_lines)
        if self.edit_mode:
            budget_prompt = build_edit_prompt(
                budget_prompt, [], include_instructions=not self.edit_protocol_sent,
                target_workspace=target_workspace,
            )
        if estimate_tokens(len(budget_prompt))[1] > attachment_plan.token_threshold:
            inventory = "\n".join(
                f"- {label}: not delivered; complete prompt metadata exceeds inline budget"
                for label in selected_labels.values()
            )
            raise ValueError(
                "Request preflight: the prompt, memory manifest, and edit instructions "
                "exceed the inline budget. No files were uploaded and no request was sent.\n"
                + inventory
            )

        with TemporaryDirectory(prefix="relaypilot-context-") as temp_dir:
            # Validated .ai sources are snapshotted rather than weakening the
            # browser provider's exclusion of arbitrary .ai directories.
            memory_uploads: dict[Path, Path] = {}
            memory_payloads: dict[Path, str] = {}
            for number, document in enumerate(memory_documents, 1):
                staged = Path(temp_dir) / f"memory-{number:03d}-{document.path.name}"
                staged.write_bytes(document.content.encode("utf-8"))
                memory_uploads[staged.resolve()] = document.path.resolve()
                memory_payloads[staged.resolve()] = document.content
            for selection_attempt in range(3):
                # A quota-specific 'fewer cells' decision restarts notebook
                # selection without submitting the original prompt.
                attached_paths.clear()
                inline_paths.clear()
                inline_contexts.clear()
                filename_overrides.clear()
                selected_notebooks = {}
                upload_paths = [p for p in attachment_plan.attachment_paths
                                if p.suffix.casefold() != '.ipynb' and p.resolve() not in memory_by_source]
                upload_paths.extend(memory_uploads)
                upload_to_original: dict[Path, Path] = dict(memory_uploads)
                inline_fallback_payloads: dict[Path, str] = dict(memory_payloads)
                filename_overrides.update({source: staged.name for staged, source in memory_uploads.items()})
                notebook_preparer = NotebookPreparationService(
                    self.file_context_builder.structured_context_builder
                ) if any(p.suffix.casefold() == '.ipynb' for p in attachment_plan.paths) else None

                try:
                    for reference in attachment_plan.references:
                        source = reference.path.resolve()
                        if source.suffix.casefold() != '.ipynb' or source in selected_notebooks:
                            continue
                        assert notebook_preparer is not None
                        selected = notebook_preparer.prepare(
                            source, f"<{reference.raw_reference}>",
                            inline_budget_chars=max(
                                0, attachment_plan.token_threshold * 3
                                - inline_fallback_base_characters(prompt, attachment_plan, memory_characters)
                                - edit_prompt_reserve - notebook_manifest_reserve - memory_manifest_reserve
                                - sum(len(item['content']) for item in inline_contexts)
                            ),
                            temporary_directory=Path(temp_dir),
                            notice=lambda text: self._notice(text, title="Notebook delivery"),
                        )
                        selected_notebooks[source] = selected
                        if selected.upload_path is not None:
                            upload = selected.upload_path.resolve()
                            upload_paths.append(upload)
                            upload_to_original[upload] = source
                            filename_overrides[source] = upload.name
                            if selected.fallback_text is not None:
                                inline_fallback_payloads[upload] = selected.fallback_text
                        else:
                            inline_paths.append(source)
                            inline_contexts.append({
                                'reference': selected.reference,
                                'path': str(source), 'content': selected.inline_text,
                            })
                except NotebookSelectionCancelled as error:
                    self._notice(str(error), title="Notebook cancelled", severity="warning")
                    return ServiceResult(state="CANCELLED", submitted=False)

                if cancel_event.is_set():
                    return ServiceResult(state="CANCELLED", submitted=False)

                if upload_paths:
                    on_state("PREPARING", "Uploading and verifying referenced files")
                    recovery_lines: list[str] = []

                    def show_recovery_line(line: str) -> None:
                        recovery_lines.append(line)

                    def request_recovery_choice(question: str) -> str:
                        explanation = "\n".join(recovery_lines)
                        recovery_lines.clear()
                        return self.request_user_input(
                            f"{explanation}\n\n{question}" if explanation else question
                        )

                    upload_decision = upload_and_confirm(
                        upload_paths, self.chat.page,
                        fallback_candidates=[
                            path for path in attachment_plan.attachment_paths
                            if path.suffix.casefold() != '.ipynb' and path.resolve() not in memory_by_source and
                            any(ref.path == path and ref.kind == FileKind.TEXT for ref in attachment_plan.references)
                        ] + list(inline_fallback_payloads),
                        inline_payloads=inline_fallback_payloads,
                        allow_notebook_reselect=bool(selected_notebooks),
                        inline_base_characters=(inline_fallback_base_characters(
                            prompt, attachment_plan, memory_characters
                        ) + edit_prompt_reserve + notebook_manifest_reserve + memory_manifest_reserve
                          + sum(len(item['content']) for item in inline_contexts)),
                        token_threshold=attachment_plan.token_threshold,
                        input_fn=request_recovery_choice,
                        output_fn=show_recovery_line,
                    )
                    if upload_decision.reason == 'RESELECT_NOTEBOOK':
                        if selection_attempt == 2:
                            self._notice("Notebook selection retry limit reached; no request sent.",
                                         title="Notebook", severity="warning")
                            return ServiceResult(state="CANCELLED", submitted=False)
                        continue

                    upload_report = upload_decision.report
                    if upload_report is not None:
                        # The service and edit validator must always see the
                        # authoritative source path, not a temporary notebook.
                        converted = []
                        for record in upload_report.records:
                            source = upload_to_original.get(record.path.resolve(), record.path.resolve())
                            converted.append(AttachmentRecord(
                                path=source, status=record.status,
                                attempts=record.attempts, message=record.message,
                                verification_method=record.verification_method,
                            ))
                        upload_report = UploadReport(upload_report.provider_name, converted)
                        from application.contracts import AttachmentStatusChanged
                        for record in upload_report.records:
                            self.emit_event(AttachmentStatusChanged(
                                attachment_id=str(record.path.resolve()),
                                file_name=record.path.name, status=record.status.value,
                                message=record.message, request_id=request_id,
                            ))
                    if not upload_decision.proceed:
                        self._notice(
                            "The prompt was not sent. Resolve any staged attachment cards "
                            "in Chrome before retrying.",
                            title="Attachment request cancelled", severity="warning",
                        )
                        return ServiceResult(state="CANCELLED", submitted=False)
                    attached_paths.extend(upload_to_original.get(Path(p).resolve(), Path(p).resolve())
                                          for p in upload_decision.attached_paths)
                    for path in upload_decision.inline_paths:
                        upload = Path(path).resolve()
                        original = upload_to_original.get(upload, upload)
                        inline_paths.append(original)
                        if upload in inline_fallback_payloads:
                            if original in memory_by_source:
                                ref = memory_by_source[original].relative_path
                            else:
                                ref = next(r.raw_reference for r in attachment_plan.references if r.path == original)
                                ref = f'<{ref}>'
                            inline_contexts.append({
                                'reference': ref, 'path': str(original),
                                'content': inline_fallback_payloads[upload],
                            })

                delivered = {p.resolve() for p in attached_paths + inline_paths}
                omitted_paths = [p for p in attachment_plan.paths if p.resolve() not in delivered]
                # Do not repeat notebook selection. Its approved exact payload
                # was already prepared above, and is never taken from a staged
                # temporary file after upload completion.
                file_context = build_inline_context(
                    self.file_context_builder, attachment_plan,
                    [p for p in inline_paths if p.suffix.casefold() != '.ipynb' and p.resolve() not in memory_by_source]
                ) + inline_contexts
                if selected_labels:
                    records = {record.path.resolve(): record for record in upload_report.records} if upload_report else {}
                    outcomes = []
                    for source, label in selected_labels.items():
                        if source in inline_paths:
                            state_label = "INLINE - complete content included"
                        elif source in attached_paths:
                            record = records.get(source)
                            state_label = "ATTACHED - verified" if record and record.status.value == "ATTACHED" else "ATTACHED - verification evidence unavailable"
                        else:
                            record = records.get(source)
                            reason = record.message if record else "not selected for delivery"
                            state_label = "NOT DELIVERED - " + reason
                        outcomes.append(f"- {label}: {state_label}")
                    self._notice("Context delivery:\n" + "\n".join(outcomes), title="Workspace files")
                delivery_prompt = build_delivery_prompt(
                    prompt, attachment_plan,
                    attached_paths=[p for p in attached_paths if p in attachment_plan.paths],
                    inline_paths=[p for p in inline_paths if p in attachment_plan.paths],
                    omitted_paths=omitted_paths, report=upload_report,
                    display_filenames=filename_overrides,
                )
                if include_memory:
                    records = {record.path.resolve(): record for record in upload_report.records} if upload_report else {}
                    memory_lines = ["PROJECT MEMORY DELIVERY", "======================="]
                    if not memory_documents:
                        memory_lines.append("No memory files are registered. No project memory was supplied.")
                    for source, document in memory_by_source.items():
                        if source in inline_paths:
                            disposition = "complete contents included inline below"
                        elif source in attached_paths:
                            record = records.get(source)
                            verified = record is not None and record.status.value == "ATTACHED"
                            evidence = "verified provider attachment" if verified else "attachment reported without verification evidence"
                            disposition = f"{evidence}: {filename_overrides[source]}"
                        else:
                            record = records.get(source)
                            reason = record.message if record else "not selected for delivery"
                            disposition = f"NOT DELIVERED: {reason}"
                        memory_lines.append(f"- {document.relative_path}: {disposition}")
                    delivery_prompt += "\n\n" + "\n".join(memory_lines)
                if selected_notebooks:
                    # Include a concise provider-neutral index manifest in
                    # ordinary and edit prompts. Unselected source/output data
                    # is never included. Full mappings are in filtered metadata.
                    origin_lines = [
                        'NOTEBOOK ORIGINAL CELL MAPPING',
                        '==============================',
                        'Notebook attachments are CONTEXT ONLY. For edits, target the original local notebook',
                        'using original zero-based indices and IDs (not filtered attachment positions).',
                    ]
                    for source, prepared in selected_notebooks.items():
                        indices = ','.join(map(str, prepared.selected_indices)) or 'none'
                        outputs = ','.join(map(str, prepared.output_indices)) or 'none'
                        reference = prepared.reference
                        origin_lines.append(
                            f'- {reference}: selected original indices [{indices}], approved outputs [{outputs}]; '
                            f'original source SHA256 {prepared.source_sha256}; '
                            f'provider file {filename_overrides.get(source, "inline context")}'
                        )
                    delivery_prompt += '\n\n' + '\n'.join(origin_lines)
                    # A workspace notebook can be edited while the user is
                    # selecting other files. Abort rather than sending stale
                    # indices to the model.
                    for source, prepared in selected_notebooks.items():
                        if sha256(source.read_bytes()).hexdigest() != prepared.source_sha256:
                            raise ValueError(f'Notebook {source.name} changed during selection; please reselect cells.')
                if self.edit_mode:
                    final_prompt = build_edit_prompt(
                        delivery_prompt, file_context,
                        include_instructions=not self.edit_protocol_sent,
                        target_workspace=target_workspace,
                    )
                else:
                    final_prompt = self.context_builder.build(
                        delivery_prompt, include_memory=False,
                        file_context=file_context,
                    )
                if estimate_tokens(len(final_prompt))[1] > attachment_plan.token_threshold:
                    raise ValueError(
                        'The complete prompt, memory, file context, and edit instructions exceed the inline budget. '
                        'No context was truncated and no request was sent. Reduce the selection; '
                        'clear staged attachment cards in Chrome before retrying.'
                    )
                break

            if cancel_event.is_set():
                return ServiceResult(state="CANCELLED", submitted=False)

            # All staging files must remain alive through send_prompt, because
            # browser-side upload confirmation and submission may be asynchronous.
            return self._send_prepared_request(
                request_id, final_prompt, cancel_event, on_state,
                on_response_start, on_response_change,
                [p for p in attached_paths if p in attachment_plan.paths],
                [p for p in inline_paths if p in attachment_plan.paths], include_memory, save_result,
                {source: delivery.source_sha256 for source, delivery in selected_notebooks.items()},
            )

    def _send_prepared_request(
        self, request_id, final_prompt, cancel_event, on_state,
        on_response_start, on_response_change,
        attached_paths, inline_paths, include_memory, save_result,
        notebook_source_fingerprints=None,
    ) -> ServiceResult:
        if cancel_event.is_set():
            return ServiceResult(state="CANCELLED", submitted=False)

        submitted = False

        def state_callback(state: str, message: str | None = None) -> None:
            nonlocal submitted
            if state == "SUBMITTED" and not submitted:
                submitted = True
                if include_memory:
                    self.memory_session.mark_memory_sent()
                    self._publish_memory_status()
                if self.edit_mode:
                    self.edit_protocol_sent = True
            on_state(state, message)

        state, result = self.chat.send_prompt(
            final_prompt,
            cancel_event=cancel_event,
            on_state=state_callback,
            on_response_start=on_response_start,
            on_response_change=on_response_change,
        )

        if state == "COMPLETED":
            if save_result:
                self._save_memory(result)
            elif self.edit_mode:
                from application.contracts import EditReviewResult

                try:
                    # Validate proposal targets against files explicitly delivered
                    # for this request. An edit targeting an omitted/failed file
                    # cannot be approved even if a model invents an insert_end
                    # operation that requires no MATCH. This is checked BEFORE
                    # the existing diff/approval writer is invoked.
                    # Edited notebook sources must not have changed since the
                    # selected cell mapping was generated for this request.
                    from hashlib import sha256
                    for source, fingerprint in (notebook_source_fingerprints or {}).items():
                        if sha256(source.read_bytes()).hexdigest() != fingerprint:
                            raise ValueError(
                                f"Notebook {source.name} changed after cell selection; "
                                "reselect cells and generate a new proposal before editing."
                            )
                    proposed_edits = self.edit_workflow.parser.parse(result)
                    if not proposed_edits:
                        raise ValueError("The model did not provide a valid edit proposal.")
                    delivered_files = {path.resolve() for path in attached_paths + inline_paths}
                    for edit in proposed_edits:
                        if edit.operation == "create":
                            # The workflow validates against the separately
                            # user-selected registered destination, never refs.
                            continue
                        target = self.workspace.resolve_path(edit.file).resolve()
                        if target not in delivered_files:
                            raise ValueError(
                                f"Edit target {edit.file} was not delivered by this request; "
                                "refusing to apply a change to a skipped file."
                            )
                    # Existing parser/validator/diff/approval writer remains the
                    # authoritative path for accepted proposals.
                    self.edit_workflow.target_workspace_id = getattr(self, "target_workspace_id", None)
                    edit_results = self.edit_workflow.run(result)
                    summaries = []
                    for item in edit_results:
                        summaries.append(f"{item['file']}: {item['status']}")
                        if item.get("message"):
                            summaries.append(f"  Error for {item['file']}: {item['message']}")
                    text = "\n".join(summaries) if summaries else "No edit operations were applied."
                    self.emit_event(EditReviewResult(
                        request_id=request_id,
                        successful=all(item['status'] != 'FAILED' for item in edit_results), message=text,
                    ))
                    if cancel_event.is_set():
                        return ServiceResult(state="CANCELLED", response=result, submitted=True)
                except Exception as error:
                    # A transient toast used to swallow these failures. Keep the
                    # precise failure visible in the conversation instead.
                    self.emit_event(EditReviewResult(
                        request_id=request_id,
                        successful=False,
                        message=(
                            f"No files were approved from this edit proposal: {error}\n"
                            "Check that the model returned a complete <AI_EDIT> "
                            "block with <EDIT>, <MATCH>, <CONTENT> and <REASON> sections."
                        ),
                    ))
        # Browser-backed failures are converted into typed runtime events by
        # GatewayRuntime. Avoid emitting a second toast from the service layer.

        return ServiceResult(state=state, response=result, submitted=submitted)

    def _save_memory(self, response: str) -> None:
        try:
            saved_files = self.memory_saver.save_from_response(response)
        except ValueError as error:
            self._notice(f"Memory proposal was rejected: {error}", title="Memory save", severity="warning")
            return
        except Exception as error:
            self._notice(f"Could not update project memory: {error}", title="Memory save", severity="error")
            return
        if not saved_files:
            self._notice("No new important information was identified.", title="Memory save")
            return
        self._notice(
            "Memory updated successfully:\n" + "\n".join(f"• {path.name}" for path in saved_files),
            title="Memory save",
            severity="information",
        )
