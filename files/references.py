"""Parse explicit workspace file references from user prompts.

This copy matches the existing RelayPilot file-reference syntax. Filesystem and
workspace validation remain the responsibility of WorkspaceManager.
"""

import re
from dataclasses import dataclass

FILE_REFERENCE_PATTERN = re.compile(r"<([^<>]+)>")


@dataclass(frozen=True)
class FileReference:
    workspace_id: int
    relative_path: str


class FileReferenceDetector:
    def detect(self, prompt):
        if not isinstance(prompt, str):
            raise TypeError("Prompt must be a string.")
        references = []
        for match in FILE_REFERENCE_PATTERN.finditer(prompt):
            raw_reference = match.group(1).strip()

            # Angle brackets also occur in ordinary prose, HTML tags, URLs and
            # programming-language syntax. Only treat path-shaped payloads as
            # workspace references. A numeric workspace prefix with a slash is
            # considered intentional syntax and receives strict validation.
            if "/" not in raw_reference:
                continue
            if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw_reference):
                continue
            if raw_reference.startswith("/"):
                # HTML closing tags such as </section> are not file references.
                continue
            if re.match(r"^[A-Za-z]:/", raw_reference):
                # Preserve Windows drive paths in explanatory prompts.
                continue

            prefix = raw_reference.split("/", 1)[0].strip()
            if not prefix.isdigit():
                raise ValueError(
                    "Invalid workspace file reference. Expected "
                    "<numeric_workspace_id/relative/path>."
                )
            references.append(self._parse_reference(raw_reference))
        return references

    @staticmethod
    def _parse_reference(raw_reference):
        if "/" not in raw_reference:
            raise ValueError("Invalid file reference. Expected <workspace_id/path>.")
        workspace_id_text, relative_path = raw_reference.split("/", 1)
        workspace_id_text = workspace_id_text.strip()
        relative_path = relative_path.strip()
        if not workspace_id_text.isdigit():
            raise ValueError("Workspace ID in file reference must be a number.")
        if not relative_path:
            raise ValueError("File path in reference cannot be empty.")
        return FileReference(int(workspace_id_text), relative_path)
