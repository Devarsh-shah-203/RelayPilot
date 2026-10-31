from collections import OrderedDict
import difflib

from files.edit_parser import EditParser, NOTEBOOK_OPERATIONS
from files.edit_validator import EditValidator
from files.edit_engine import EditEngine
from files.diff_generator import DiffGenerator
from files.notebook_diff import NotebookDiffFormatter
from files.structured_edit import StructuredEditAdapter
from files.writer import FileWriter


class EditWorkflow:
    """Sequential approved CREATE/MODIFY, with grouped notebook cell reviews."""

    def __init__(self, workspace_manager, approval_function, target_workspace_id=None):
        self.parser = EditParser()
        self.structured_edit_adapter = StructuredEditAdapter()
        self.validator = EditValidator(workspace_manager, structured_edit_adapter=self.structured_edit_adapter)
        self.engine = EditEngine(self.structured_edit_adapter)
        self.diff_generator = DiffGenerator()
        self.notebook_diff_formatter = NotebookDiffFormatter()
        self.writer = FileWriter()
        self.approval_function = approval_function
        self.target_workspace_id = target_workspace_id

    def run(self, response):
        edits = self.parser.parse(response)
        if not edits:
            raise ValueError("The model returned no edits.")
        self.validator.target_workspace_id = self.target_workspace_id
        notebook_groups = OrderedDict()
        text_edits = []
        staged_creates = {}
        for edit in edits:
            if edit.operation == "create":
                validated = self.validator.validate(edit)
                if validated.path in staged_creates:
                    raise ValueError(f"Duplicate CREATE target: {edit.file}")
                staged_creates[validated.path] = validated
                text_edits.append((edit, validated))
            elif edit.operation in NOTEBOOK_OPERATIONS:
                validated = self.validator.validate(edit)
                key = validated.path
                if key not in notebook_groups:
                    notebook_groups[key] = {
                        "path": validated.path, "original": validated.original_text,
                        "validated": validated, "edits": [], "file_display": edit.file,
                    }
                notebook_groups[key]["edits"].append(edit)
            else:
                text_edits.append((edit, None))

        staged_notebooks = []
        for group in notebook_groups.values():
            proposed = self.engine.apply_structured_batch(group["path"], group["original"], group["edits"])
            diff = self.notebook_diff_formatter.generate(group["original"], proposed, group["file_display"], group["edits"])
            if not diff:
                raise ValueError(f"Notebook edit produced no changes: {group['file_display']}")
            staged_notebooks.append({**group, "proposed": proposed, "diff": diff,
                                     "reason": "\n".join(f"- {e.reason}" for e in group["edits"])})

        results = []
        for edit, prevalidated in text_edits:
            approval_requested = False
            try:
                validated = prevalidated or self.validator.validate(edit)
                creating = edit.operation == "create"
                original = "" if creating else validated.original_text
                proposed = self.engine.apply(validated, original)
                if creating:
                    diff = "".join(difflib.unified_diff([], proposed.splitlines(keepends=True),
                                                       fromfile="/dev/null", tofile=f"b/{edit.file}"))
                    if not diff:
                        diff = f"--- /dev/null\n+++ b/{edit.file}\n(New empty file)\n"
                    reason = f"New file request\n{edit.reason}"
                else:
                    diff = self.diff_generator.generate(original, proposed, edit.file)
                    reason = edit.reason
                    if not diff:
                        raise ValueError(f"Edit produced no changes: {edit.file}")
                approval_requested = True
                if not self.approval_function(edit.file, reason, diff):
                    results.append({"file": edit.file, "status": "REJECTED"})
                    continue
                self.writer.write_reviewed(validated, proposed, create=creating)
                results.append({"file": edit.file, "status": "APPLIED"})
            except Exception as error:
                if not results and not approval_requested:
                    raise
                results.append({"file": edit.file, "status": "FAILED", "message": str(error)})

        for group in staged_notebooks:
            try:
                if not self.approval_function(group["file_display"], group["reason"], group["diff"]):
                    results.append({"file": group["file_display"], "status": "REJECTED"})
                    continue
                self.writer.write_reviewed(group["validated"], group["proposed"])
                results.append({"file": group["file_display"], "status": "APPLIED"})
            except Exception as error:
                results.append({"file": group["file_display"], "status": "FAILED", "message": str(error)})
        return results
