from pathlib import Path


class FileWriter:

    def write(
        self,
        path: Path,
        content: str,
    ) -> None:

        if not isinstance(path, Path):
            path = Path(path)

        path = path.resolve()

        if not path.exists():
            raise FileNotFoundError(
                f"File does not exist: {path}"
            )

        if not path.is_file():
            raise ValueError(
                f"Path is not a file: {path}"
            )

        try:
            path.write_text(
                content,
                encoding="utf-8",
            )
        except PermissionError as error:
            raise PermissionError(
                f"Permission denied: {path}"
            ) from error

    def write_exact_text(
        self,
        path: Path,
        content: str,
        *,
        expected_content: str | None = None,
    ) -> None:
        """Write UTF-8 bytes without newline translation.

        ``expected_content`` enables a compare-before-write guard for reviewed
        edits: if the on-disk bytes changed after the diff was approved, refuse
        to overwrite the newer contents. The existing ``write`` API is unchanged.
        """
        if not isinstance(path, Path):
            path = Path(path)
        path = path.resolve()
        if not path.exists():
            raise FileNotFoundError(f"File does not exist: {path}")
        if not path.is_file():
            raise ValueError(f"Path is not a file: {path}")

        if expected_content is not None:
            expected_bytes = expected_content.encode("utf-8")
            try:
                current_bytes = path.read_bytes()
            except PermissionError as error:
                raise PermissionError(f"Permission denied: {path}") from error
            if current_bytes != expected_bytes:
                raise RuntimeError(
                    f"File changed after its diff was reviewed: {path}. Refusing to overwrite it."
                )

        try:
            path.write_bytes(content.encode("utf-8"))
        except PermissionError as error:
            raise PermissionError(f"Permission denied: {path}") from error


    def write_reviewed(self, validated_edit, content: str, *, create=False) -> None:
        """Apply the reviewed bytes through pinned, non-symlink directory handles."""
        from files.safe_paths import safe_write_bytes
        if validated_edit.safe_target is None:
            raise ValueError("A safe workspace target is required for approved writes.")
        from files.edit_parser import NOTEBOOK_OPERATIONS
        original = validated_edit.original_bytes or b""
        if not create and validated_edit.edit.operation not in NOTEBOOK_OPERATIONS:
            # Validation matches logical text; retain a uniformly used source
            # newline convention when encoding the approved ordinary-file edit.
            if b"\r\n" in original and b"\n" not in original.replace(b"\r\n", b""):
                content = content.replace("\r\n", "\n").replace("\n", "\r\n")
            elif b"\r" in original and b"\n" not in original:
                content = content.replace("\r\n", "\n").replace("\n", "\r")
        safe_write_bytes(
            validated_edit.safe_target, content.encode("utf-8"),
            expected_bytes=validated_edit.original_bytes, create=create,
        )
