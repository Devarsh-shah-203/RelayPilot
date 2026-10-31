from dataclasses import dataclass
from pathlib import Path
import io
import json

from files.edit_parser import FileEdit, ALLOWED_OPERATIONS, NOTEBOOK_OPERATIONS
from files.structured_edit import StructuredEditAdapter
from files.reader import FileReader
from files.safe_paths import SafeTarget, prepare_target, safe_read_bytes
from workspace.manager import WorkspaceManager


@dataclass(frozen=True)
class ValidatedEdit:
    edit: FileEdit
    path: Path
    match_start: int
    match_end: int
    safe_target: SafeTarget | None = None
    original_bytes: bytes | None = None

    @property
    def original_text(self):
        raw = (self.original_bytes or b"").decode("utf-8")
        if self.edit.operation in NOTEBOOK_OPERATIONS:
            return raw
        return io.StringIO(raw, newline=None).read()


class EditValidator:
    def __init__(self, workspace_manager: WorkspaceManager,
                 file_reader: FileReader | None = None,
                 structured_edit_adapter: StructuredEditAdapter | None = None,
                 target_workspace_id=None):
        self.workspace_manager = workspace_manager
        self.file_reader = file_reader or FileReader()
        self.structured_edit_adapter = structured_edit_adapter or StructuredEditAdapter()
        self.target_workspace_id = target_workspace_id

    def validate(self, edit: FileEdit) -> ValidatedEdit:
        if edit.operation not in ALLOWED_OPERATIONS:
            raise ValueError(f"Unsupported edit operation: {edit.operation}")
        creating = edit.operation == "create"
        target = prepare_target(self.workspace_manager, edit.file,
                                selected_workspace_id=self.target_workspace_id, creating=creating)
        if creating:
            if edit.match:
                raise ValueError("CREATE must have an empty MATCH.")
            return ValidatedEdit(edit, target.path, 0, 0, target, None)

        original_bytes = safe_read_bytes(target)
        try:
            raw_content = original_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"File is not valid UTF-8 text: {target.path}") from error
        is_structured = self.structured_edit_adapter.supports(target.path)
        if edit.operation in NOTEBOOK_OPERATIONS:
            if not is_structured:
                raise ValueError(f"Operation '{edit.operation}' requires a structured document adapter.")
            try:
                notebook = json.loads(raw_content)
            except json.JSONDecodeError as error:
                raise ValueError(f"Notebook is not valid JSON (line {error.lineno}, column {error.colno}).") from error
            if not isinstance(notebook, dict):
                raise ValueError("Notebook JSON must contain a top-level object.")
            self.structured_edit_adapter.validate_edit(target.path, edit, notebook)
            return ValidatedEdit(edit, target.path, 0, 0, target, original_bytes)

        if is_structured:
            raise ValueError(f"{target.path.suffix} must be edited with a format-aware structured operation; raw text edits of structured document JSON are disabled.")
        content = io.StringIO(raw_content, newline=None).read()
        if edit.operation == "modify":
            if edit.match != content:
                raise ValueError("MODIFY requires MATCH containing the complete current file.")
            return ValidatedEdit(edit, target.path, 0, len(content), target, original_bytes)
        if edit.operation in {"insert_start", "insert_end"}:
            if edit.match:
                raise ValueError(f"{edit.operation} requires an empty MATCH.")
            return ValidatedEdit(edit, target.path, 0, 0, target, original_bytes)
        matches = self._find_matches(content, edit.match)
        if not matches:
            raise ValueError(f"No match found in {edit.file}.")
        if len(matches) > 1:
            raise ValueError(f"Ambiguous match in {edit.file}: {len(matches)} matches found.")
        start, end = matches[0]
        return ValidatedEdit(edit, target.path, start, end, target, original_bytes)

    @staticmethod
    def _find_matches(content: str, match: str) -> list[tuple[int, int]]:
        if not match:
            raise ValueError("Match cannot be empty.")
        matches = []
        start = 0
        while True:
            index = content.find(match, start)
            if index == -1:
                break
            matches.append((index, index + len(match)))
            start = index + len(match)
        return matches

    @staticmethod
    def _validate_position_operation(edit, path, content):
        return ValidatedEdit(edit, path, 0, 0)
