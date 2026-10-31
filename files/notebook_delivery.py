"""Provider-neutral, one-time notebook cell selection and delivery preparation.

Temporary notebooks carry only explicitly selected cells and approved outputs.
They are never edit targets; the original local notebook remains authoritative.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable

from attachments.policy import estimate_tokens
from files.structured_context import StructuredContextBuilder, NotebookSelectionCancelled


@dataclass(frozen=True)
class NotebookDelivery:
    source: Path
    reference: str
    upload_path: Path | None
    inline_text: str | None
    fallback_text: str | None
    selected_indices: tuple[int, ...]
    output_indices: tuple[int, ...]
    source_sha256: str
    approximate_tokens: int


class NotebookPreparationService:
    """Select cells and outputs once, then automatically choose delivery."""

    def __init__(self, builder: StructuredContextBuilder) -> None:
        self.builder = builder

    def prepare(
        self,
        path: Path,
        reference: str,
        *,
        inline_budget_chars: int,
        temporary_directory: Path,
        notice: Callable[[str], None],
        choose: Callable[[str], str] | None = None,
    ) -> NotebookDelivery:
        """Prepare delivery using only cell and output questions.

        ``choose`` is accepted for compatibility with older callers but is
        intentionally unused: routine attachment vs inline selection is automatic.
        """
        path = Path(path).resolve()
        original_bytes = path.read_bytes()
        try:
            notebook = json.loads(original_bytes.decode('utf-8-sig'))
        except (UnicodeError, ValueError) as exc:
            raise ValueError(f"Invalid notebook {path.name}: {exc}") from exc
        if not isinstance(notebook, dict) or not isinstance(notebook.get('cells'), list):
            raise ValueError(f"Notebook {path.name} must contain a cells array")
        cells = notebook['cells']
        if any(not isinstance(cell, dict) for cell in cells):
            raise ValueError("Notebook cells must be objects")
        all_indices = set(range(len(cells)))
        self.builder._show_notebook_summary(path, cells, notebook)
        budget = max(0, int(inline_budget_chars))

        # There are exactly two user decisions: which cells and which outputs.
        # Delivery mode is always computed from the fully serialized selected
        # context, not requested via another UPLOAD / FEWER / CANCEL dialog.
        included = self.builder._ask_indices(
            'Include notebook cells [all / none / indices or ranges] (default: all): ',
            allowed=all_indices, default=all_indices, label='cell selection',
        )
        outputs = self.builder._ask_indices(
            'Include outputs [none / all / indices or ranges] (default: none): ',
            allowed=included, default=set(), label='output selection',
        ) if included else set()

        text = self.builder._format_context(
            notebook=notebook, cells=cells, reference=reference, path=path,
            included_cells=included, output_cells=outputs,
        )
        # Estimate actual inline context including its cell index map, source
        # text, metadata labels, and any approved rendered outputs.
        estimated = estimate_tokens(len(text))[1]
        within = len(text) <= budget and estimated <= budget // 3
        has_media = any(_contains_media(cells[i]) for i in outputs)
        if has_media:
            notice(
                f"{path.name}: approved image/media outputs cannot be represented "
                "fully as plain text; delivering them as a notebook attachment."
            )
        if any(cells[i].get('attachments') for i in included - outputs):
            notice(
                f"{path.name}: embedded Markdown attachments not explicitly approved "
                "as outputs are excluded from delivery."
            )
        digest = sha256(original_bytes).hexdigest()
        selected_indices = tuple(sorted(included))
        output_indices = tuple(sorted(outputs))

        if within and not has_media:
            # Even all-cell selections stay inline when comfortably below the
            # threshold; a large notebook or rich media gets a real attachment.
            notice(f"{path.name}: selected notebook context fits inline budget; sending selected cells in the prompt.")
            return NotebookDelivery(
                path, reference, None, text, None,
                selected_indices, output_indices, digest, estimated,
            )

        # Preserve the user's selection even if the original notebook is large.
        # The original file is safe to upload only when every cell AND every
        # stored output (including media) has explicitly been approved.
        all_selected = included == all_indices
        all_outputs_approved = outputs == all_indices
        if all_selected and all_outputs_approved:
            upload = path
            notice(f"{path.name}: inline threshold exceeded or rich output selected; uploading original notebook.")
        else:
            upload = self._make_filtered(
                notebook, cells, included, outputs, digest, path,
                temporary_directory,
            )
            notice(
                f"{path.name}: inline threshold exceeded or rich output selected; "
                "uploading a temporary notebook with only approved cells and outputs."
            )
        return NotebookDelivery(
            path, reference, upload, None, text if within and not has_media else None,
            selected_indices, output_indices, digest, estimated,
        )

    @staticmethod
    def _make_filtered(notebook, cells, included, outputs, digest, path, directory) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        # Write to a unique temporary path without using source directory state.
        import tempfile
        with tempfile.NamedTemporaryFile(
            suffix='.ipynb', prefix=f'{path.stem}_selected_', dir=directory,
            mode='w', encoding='utf-8', delete=False,
        ) as dest:
            target = Path(dest.name)
            filtered = {
                'nbformat': notebook.get('nbformat', 4),
                'nbformat_minor': notebook.get('nbformat_minor', 5),
                'metadata': {},
                'cells': [],
            }
            meta = notebook.get('metadata', {})
            if isinstance(meta, dict):
                for key in ('kernelspec', 'language_info'):
                    if key in meta:
                        filtered['metadata'][key] = deepcopy(meta[key])
            mapping = []
            for i in sorted(included):
                cell = deepcopy(cells[i])
                # Never include outputs of unapproved cells.
                if i not in outputs and cell.get('cell_type') == 'code':
                    cell['outputs'] = []
                    cell['execution_count'] = None
                if i not in outputs and 'attachments' in cell:
                    # Markdown cell attachment MIME bundles are not code outputs,
                    # but are still embedded media; exclude when unapproved.
                    cell['attachments'] = {}
                filtered['cells'].append(cell)
                mapping.append({
                    'original_index': i,
                    'cell_id': cell.get('id'),
                    'source_sha256': sha256(
                        StructuredContextBuilder._source_text(cell.get('source', '')).encode('utf-8')
                    ).hexdigest(),
                })
            filtered['metadata']['relaypilot_origin'] = {
                'source_name': path.name, 'source_sha256': digest,
                'original_cell_count': len(cells), 'cell_index_map': mapping,
                'approved_output_indices': sorted(outputs),
                'context_only': True,
            }
            json.dump(filtered, dest, ensure_ascii=False)
        return target


def _contains_media(cell: dict) -> bool:
    if cell.get('attachments'):
        return True
    for output in cell.get('outputs', []) if isinstance(cell.get('outputs'), list) else []:
        if not isinstance(output, dict):
            continue
        data = output.get('data', {})
        if isinstance(data, dict) and any(
            str(mime).lower().startswith(('image/', 'audio/', 'video/'))
            or str(mime).lower() in ('application/pdf', 'application/octet-stream')
            or (str(mime).lower() == 'text/html' and 'data:image/' in str(data[mime]))
            for mime in data
        ):
            return True
    return False
