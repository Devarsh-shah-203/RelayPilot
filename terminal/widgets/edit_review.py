"""Sequential, collapsible edit reviews for RelayPilot's controlled edit workflow.

The widget presents a validated proposal; it never changes files. The
existing EditWorkflow owns validation, approval resolution, and writing.
"""

from __future__ import annotations

from rich.syntax import Syntax
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Collapsible, Static


class InlineEditReview(Vertical):
    """Legacy-style proposed edit with a collapsible code/diff section.

    The approval question and controls remain visible when the diff is folded.
    Every proposal has its own disclosure state; folding a diff has no effect
    on parsing, validation or the writer's approval decision.
    """

    DEFAULT_CSS = """
    InlineEditReview {
        width: 100%; height: auto; margin: 1 0 2 0;
        padding: 1 2 1 2; background: #121519;
        border-top: solid #3a444a;
        border-bottom: solid #3a444a;
    }
    InlineEditReview .edit-review-title {
        width: 100%; height: auto; color: #dce8df;
        text-style: bold; margin-bottom: 1;
    }
    InlineEditReview .edit-section-title {
        height: 1; margin-top: 1; color: #98a7b5; text-style: bold;
    }
    InlineEditReview .edit-reason {
        width: 100%; height: auto; color: #e0e2e4;
    }
    InlineEditReview .edit-code-dropdown {
        width: 100%; height: auto; margin: 1 0;
        padding: 0; background: transparent; border: none;
    }
    InlineEditReview .edit-code-dropdown CollapsibleTitle {
        width: 100%; height: 3; padding: 1 2;
        background: #20262b; color: #cbd5da;
        text-style: bold;
    }
    InlineEditReview .edit-code-dropdown CollapsibleTitle:hover,
    InlineEditReview .edit-code-dropdown CollapsibleTitle:focus {
        background: #303c43; color: #eef2f3;
    }
    InlineEditReview .edit-code-dropdown Contents {
        width: 100%; height: auto; padding: 0;
    }
    InlineEditReview .edit-diff {
        width: 100%; height: auto; margin: 0;
        padding: 1 2; background: #1c2024;
    }
    InlineEditReview .edit-question {
        width: 100%; height: 1; margin-top: 1;
        color: #ebcfaa; text-style: bold;
    }
    InlineEditReview .edit-review-controls {
        height: 3; width: 100%; align-horizontal: left;
        margin: 1 0 0 0;
    }
    InlineEditReview .edit-review-controls Button {
        height: 3; min-width: 14; width: auto;
        margin-right: 1; padding: 0 2; border: none;
        background: #252b30; color: #e0e5e9;
        text-style: bold;
    }
    InlineEditReview .edit-review-controls Button:hover,
    InlineEditReview .edit-review-controls Button:focus {
        background: #37434a;
    }
    InlineEditReview .edit-review-controls .edit-apply-button {
        background: #284a3d; color: #dbf5e4;
    }
    InlineEditReview .edit-review-controls .edit-apply-button:hover,
    InlineEditReview .edit-review-controls .edit-apply-button:focus {
        background: #376a52;
    }
    InlineEditReview .edit-decision {
        height: auto; width: 100%; margin: 1 0;
        color: #96b3a5;
    }
    """

    def __init__(self, approval_id: str, file_name: str, reason: str, diff: str) -> None:
        super().__init__(classes="inline-edit-review")
        self.approval_id = approval_id
        self.file_name = file_name
        self.reason = reason
        self.diff = diff
        self.show_code = False  # Each edit starts collapsed.
        self.decision: bool | None = None
        self.result: str | None = None

    @staticmethod
    def _change_summary(diff: str) -> str:
        """One-line change counts for the collapsed dropdown label."""
        added = sum(line.startswith("+") and not line.startswith("+++") for line in diff.splitlines())
        removed = sum(line.startswith("-") and not line.startswith("---") for line in diff.splitlines())
        return f"+{added} / -{removed}"

    def _dropdown_title(self, expanded: bool) -> str:
        action = "Hide code changes" if expanded else "Show code changes"
        return f"{action}  ·  {self._change_summary(self.diff)}"

    def compose(self):
        title = "NEW FILE REQUEST" if self.reason.startswith("New file request\n") else "PROPOSED EDIT"
        yield Static(f"{title}: {self.file_name}", classes="edit-review-title", markup=False)
        yield Static("Reason:", classes="edit-section-title", markup=False)
        yield Static(self.reason or "No reason supplied.", classes="edit-reason", markup=False)
        yield Static("Changes:", classes="edit-section-title", markup=False)
        # A disclosure row, not an on/off switch. The full original diff remains
        # available under the same proposal and can be reopened after a decision.
        with Collapsible(
            title=self._dropdown_title(expanded=False),
            collapsed=True,
            collapsed_symbol="▸",
            expanded_symbol="▾",
            id=f"code-review-{self.approval_id}",
            classes="edit-code-dropdown",
        ):
            yield Static(
                Syntax(self.diff, "diff", theme="ansi_dark", line_numbers=False, word_wrap=True),
                classes="edit-diff", markup=False,
            )
        yield Static("Apply this change? [y/N]", classes="edit-question", markup=False)
        with Horizontal(classes="edit-review-controls"):
            yield Button("Reject", id=f"reject-review-{self.approval_id}")
            yield Button(
                "Apply change", id=f"apply-review-{self.approval_id}",
                classes="edit-apply-button",
            )
        yield Static(
            "Review the diff before approving. Nothing is written until you approve.",
            classes="edit-decision", markup=False,
        )

    def on_collapsible_expanded(self, event: Collapsible.Expanded) -> None:
        if event.collapsible.id != f"code-review-{self.approval_id}":
            return
        self.show_code = True
        event.collapsible.title = self._dropdown_title(expanded=True)
        event.stop()

    def on_collapsible_collapsed(self, event: Collapsible.Collapsed) -> None:
        if event.collapsible.id != f"code-review-{self.approval_id}":
            return
        self.show_code = False
        event.collapsible.title = self._dropdown_title(expanded=False)
        event.stop()

    def _hide_controls(self) -> None:
        self.query_one(".edit-review-controls", Horizontal).display = False

    def mark_decision(self, approved: bool) -> None:
        """Record a decision without claiming it was successfully written."""
        self.decision = approved
        self._hide_controls()
        self.query_one(".edit-decision", Static).update(
            "Approved — applying validated change…" if approved
            else "Rejected — no change will be written."
        )

    def mark_result(self, result: str) -> None:
        """Display the *writer's* final outcome, after the workflow finishes."""
        self.result = result
        self._hide_controls()
        label = {
            "APPLIED": "✓ APPLIED — file updated",
            "REJECTED": "✕ REJECTED — file unchanged",
            "FAILED": "FAILED - inspect this file; see the error below",
        }.get(result, result)
        self.query_one(".edit-decision", Static).update(label)


class EditReviewActions(Horizontal):
    """Sticky actions stay accessible while scrolling a long diff."""

    DEFAULT_CSS = """
    EditReviewActions {
        display: none;
        height: 5; width: 100%; padding: 1 2;
        background: #171d1d; border-top: solid #455a4e;
        align-vertical: middle;
    }
    EditReviewActions #review-actions-label {
        height: 1; width: 1fr; min-width: 0;
        color: #d6dfd3; content-align: left middle;
    }
    EditReviewActions Button {
        height: 3; width: auto; min-width: 14;
        margin-left: 1; padding: 0 2; border: none;
        background: #252b30; color: #e0e5e9;
        text-style: bold;
    }
    EditReviewActions Button:hover,
    EditReviewActions Button:focus {
        background: #37434a;
    }
    EditReviewActions #apply-edit-button {
        background: #284a3d; color: #dbf5e4;
    }
    EditReviewActions #apply-edit-button:hover,
    EditReviewActions #apply-edit-button:focus {
        background: #376a52;
    }
    """

    def compose(self):
        yield Static("Review pending · Apply this change? [y/N]", id="review-actions-label", markup=False)
        yield Button("Reject", id="reject-edit-button")
        yield Button("Apply change", id="apply-edit-button")

    def open_for(self, file_name: str) -> None:
        self.query_one("#review-actions-label", Static).update(
            f"REVIEW: {file_name}  ·  Apply this change? [y/N]"
        )
        self.display = True
        self.query_one("#reject-edit-button", Button).disabled = False
        self.query_one("#apply-edit-button", Button).disabled = False

    def close(self) -> None:
        self.display = False
