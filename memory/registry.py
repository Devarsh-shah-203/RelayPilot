"""Project-local Markdown memory registry and safe, explicit file management.

The content lives in .ai; .relaypilot/memory.json only identifies eligible
files. Reading never creates or automatically registers memory files.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class MemoryDocument:
    path: Path
    relative_path: str
    content: str


@dataclass(frozen=True, slots=True)
class MemoryRenewal:
    added: tuple[str, ...]
    warnings: tuple[str, ...]
    missing: tuple[str, ...]


def _is_redirect(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def _identity(info) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name or name != name.strip():
        raise ValueError("Memory filename must be a nonempty name.")
    if any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name):
        raise ValueError("Memory filename must not contain paths or reserved characters.")
    if name.endswith((" ", ".")) or name.casefold() == ".md":
        raise ValueError("Memory filename is invalid.")
    suffix = Path(name).suffix
    if not suffix:
        name += ".md"
    elif suffix.casefold() != ".md":
        raise ValueError("Memory files use the .md extension; omit the extension or use .md.")
    reserved = {"con", "prn", "aux", "nul"} | {
        f"{prefix}{index}" for prefix in ("com", "lpt") for index in range(1, 10)
    }
    if name.split(".", 1)[0].casefold() in reserved:
        raise ValueError("Memory filename is reserved by the filesystem.")
    return name


class _DirectoryLease:
    """Keep writes anchored to checked directories for the entire operation."""

    def __init__(self, registry, *, create: bool):
        self.registry = registry
        self.create = create
        self.descriptors: dict[Path, int] = {}
        self.handles: dict[Path, int] = {}
        self.identities: dict[Path, tuple[int, int]] = {}

    def __enter__(self):
        try:
            self._acquire(self.registry.project_root)
            self.check()
            for directory in (self.registry.memory_dir, self.registry.registry_path.parent):
                if _is_redirect(directory):
                    raise ValueError(f"Memory directory must not be a symlink or junction: {directory}")
                if not directory.exists() and self.create:
                    if os.name == "nt":
                        directory.mkdir()
                    else:
                        os.mkdir(directory.name, dir_fd=self.descriptors[self.registry.project_root])
                if directory.exists():
                    self._acquire(directory)
            self.check()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _acquire(self, path: Path) -> None:
        if _is_redirect(path) or not path.is_dir():
            raise ValueError(f"Unsafe memory directory: {path}")
        before = path.lstat()
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateFileW.argtypes = [
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
            ]
            kernel.CreateFileW.restype = wintypes.HANDLE
            # Directory read access, read/write sharing, OPEN_EXISTING, and do not
            # traverse a reparse point. Denying delete sharing prevents replacing
            # a held directory; write sharing permits atomic child renames.
            handle = kernel.CreateFileW(
                str(path), 0x80000000, 0x3, None, 3, 0x02000000 | 0x00200000, None
            )
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            self.handles[path] = handle
        else:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            if path == self.registry.project_root:
                descriptor = os.open(path, flags)
            else:
                descriptor = os.open(
                    path.name, flags,
                    dir_fd=self.descriptors[self.registry.project_root],
                )
            self.descriptors[path] = descriptor
            if _identity(os.fstat(descriptor)) != _identity(before):
                raise ValueError(f"Memory directory changed while opening: {path}")
        self.identities[path] = _identity(before)

    def check(self) -> None:
        for directory, expected in self.identities.items():
            if _is_redirect(directory) or not directory.is_dir():
                raise ValueError(f"Memory directory was redirected: {directory}")
            if _identity(directory.lstat()) != expected:
                raise ValueError(f"Memory directory changed during the operation: {directory}")
            if directory.resolve(strict=True) != directory:
                raise ValueError(f"Memory directory no longer has its approved path: {directory}")
        if self.identities[self.registry.project_root] != self.registry._root_identity:
            raise ValueError("The selected project root changed; select the project again.")

    def open_file(self, path: Path, flags: int) -> int:
        self.check()
        if path.parent not in self.identities:
            raise FileNotFoundError(f"Memory directory is missing: {path.parent}")
        if _is_redirect(path):
            raise ValueError(f"Memory paths must not be symlinks or junctions: {path}")
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if os.name == "nt":
            return os.open(path, flags, 0o600)
        return os.open(path.name, flags, 0o600, dir_fd=self.descriptors[path.parent])

    def replace(self, source: Path, destination: Path) -> None:
        self.check()
        if _is_redirect(destination):
            raise ValueError("Memory registry must not be a symlink or junction.")
        if destination.exists() and not destination.is_file():
            raise ValueError("Memory registry is not a regular file.")
        if os.name == "nt":
            os.replace(source, destination)
        else:
            directory_fd = self.descriptors[source.parent]
            os.replace(
                source.name, destination.name,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
            )
        self.check()


    def remove_temporary(self, path: Path) -> None:
        if os.name == "nt":
            path.unlink(missing_ok=True)
        else:
            try:
                os.unlink(path.name, dir_fd=self.descriptors[path.parent])
            except FileNotFoundError:
                pass

    def __exit__(self, *_args):
        for descriptor in self.descriptors.values():
            os.close(descriptor)
        self.descriptors.clear()
        if self.handles:
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            for handle in reversed(list(self.handles.values())):
                kernel.CloseHandle(handle)
            self.handles.clear()


class MemoryRegistry:
    def __init__(self, project_root):
        requested = Path(project_root).expanduser().absolute()
        if _is_redirect(requested):
            raise ValueError("The project root must not be a symlink or junction.")
        self.project_root = requested.resolve(strict=True)
        if self.project_root != requested:
            raise ValueError("The project root must not be redirected by an ancestor path.")
        if not self.project_root.is_dir():
            raise ValueError("The selected project root must be an existing directory.")
        self._root_identity = _identity(self.project_root.lstat())
        self.memory_dir = self.project_root / ".ai"
        self.registry_path = self.project_root / ".relaypilot" / "memory.json"

    def _relative(self, value: str) -> str:
        if not isinstance(value, str) or not value.startswith(".ai/"):
            raise ValueError("Registered memory paths must be direct .ai/*.md paths.")
        name = _validate_name(value[4:])
        return f".ai/{name}"

    def _document_path(self, relative: str, lease: _DirectoryLease) -> Path:
        relative = self._relative(relative)
        lease.check()
        path = self.project_root / relative
        if _is_redirect(path):
            raise ValueError(f"Registered memory must not be a symlink or junction: {relative}")
        if not path.exists():
            raise FileNotFoundError(f"Registered memory file is missing: {relative}. Run memory renew.")
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError(f"Registered memory is not a regular file: {relative}")
        if path.resolve(strict=True).parent != self.memory_dir:
            raise ValueError(f"Registered memory escapes .ai: {relative}")
        return path

    def _read(self, path: Path, lease: _DirectoryLease) -> bytes:
        if _is_redirect(path):
            raise ValueError(f"Memory paths must not be symlinks or junctions: {path}")
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Memory path is not a regular file: {path}")
        descriptor = lease.open_file(path, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if _identity(opened) != _identity(before):
                raise ValueError(f"Memory file changed while opening: {path.name}")
            lease.check()
            if _is_redirect(path) or _identity(path.lstat()) != _identity(opened):
                raise ValueError(f"Memory file was redirected: {path.name}")
            data = stream.read()
            after = os.fstat(stream.fileno())
        lease.check()
        if _is_redirect(path) or _identity(path.lstat()) != _identity(opened):
            raise ValueError(f"Memory file changed during reading: {path.name}")
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f"Memory file changed during reading: {path.name}; retry the request.")
        return data

    def _load_entries(self, lease: _DirectoryLease) -> list[dict]:
        if _is_redirect(self.registry_path):
            raise ValueError("Memory registry must not be a symlink or junction.")
        if not self.registry_path.exists():
            return []
        try:
            document = json.loads(self._read(self.registry_path, lease).decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Memory registry contains invalid JSON: {error}") from error
        if not isinstance(document, dict) or type(document.get("version")) is not int or document["version"] != 1:
            raise ValueError("Memory registry must have supported version 1.")
        entries = document.get("memories")
        if not isinstance(entries, list):
            raise ValueError("Memory registry must contain a memories list.")
        checked = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or type(entry.get("enabled")) is not bool:
                raise ValueError("Each registered memory needs a path and a boolean enabled field.")
            relative = self._relative(entry.get("path"))
            if relative.casefold() in seen:
                raise ValueError(f"Duplicate memory registry entry: {relative}")
            seen.add(relative.casefold())
            checked.append({"path": relative, "enabled": entry["enabled"]})
        return checked

    def list_entries(self, *, enabled_only: bool = False) -> list[dict]:
        with _DirectoryLease(self, create=False) as lease:
            entries = self._load_entries(lease)
            return [entry for entry in entries if entry["enabled"] or not enabled_only]

    def load_current(self) -> list[MemoryDocument]:
        with _DirectoryLease(self, create=False) as lease:
            documents = []
            for entry in self._load_entries(lease):
                if not entry["enabled"]:
                    continue
                path = self._document_path(entry["path"], lease)
                try:
                    content = self._read(path, lease).decode("utf-8-sig")
                except UnicodeError as error:
                    raise ValueError(f"Memory file is not valid UTF-8: {entry['path']}") from error
                documents.append(MemoryDocument(path, entry["path"], content))
            return documents

    def _write_registry(self, entries: list[dict], lease: _DirectoryLease) -> None:
        temporary = self.registry_path.with_name(f".memory-{uuid4().hex}.tmp")
        descriptor = lease.open_file(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                payload = json.dumps(
                    {"version": 1, "memories": entries}, indent=2, ensure_ascii=False
                ) + "\n"
                stream.write(payload.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            lease.replace(temporary, self.registry_path)
        finally:
            lease.remove_temporary(temporary)

    def add(self, name: str, content: str = "") -> Path:
        name = _validate_name(name)
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("Initial memory content must be Markdown text without null bytes.")
        relative = f".ai/{name}"
        with _DirectoryLease(self, create=True) as lease:
            entries = self._load_entries(lease)
            if any(entry["path"].casefold() == relative.casefold() for entry in entries):
                raise FileExistsError(f"Memory is already registered: {name}")
            # Case-insensitive duplicate protection also keeps registries portable.
            if any(path.name.casefold() == name.casefold() for path in self.memory_dir.iterdir()):
                raise FileExistsError(f"Memory file already exists: {name}. Use memory renew to register it.")
            path = self.memory_dir / name
            descriptor = lease.open_file(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(descriptor, "wb") as stream:
                lease.check()
                if _is_redirect(path) or _identity(path.lstat()) != _identity(os.fstat(stream.fileno())):
                    raise ValueError("Memory path changed before writing.")
                stream.write(content.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            lease.check()
            try:
                self._write_registry(entries + [{"path": relative, "enabled": True}], lease)
            except Exception as error:
                raise OSError(
                    f"Memory file {name} was created but could not be registered: {error}. "
                    "Run memory renew after fixing the registry."
                ) from error
            return path

    def renew(self) -> MemoryRenewal:
        with _DirectoryLease(self, create=True) as lease:
            entries = self._load_entries(lease)
            known = {entry["path"].casefold() for entry in entries}
            warnings = []
            missing = []
            for entry in entries:
                try:
                    self._document_path(entry["path"], lease)
                except FileNotFoundError:
                    missing.append(entry["path"])
                    warnings.append(f"Registered memory file is missing: {entry['path']}")
                except ValueError as error:
                    warnings.append(str(error))
            added = []
            for path in sorted(self.memory_dir.iterdir(), key=lambda item: (item.name.casefold(), item.name)):
                if _is_redirect(path):
                    warnings.append(f"Unsafe memory entry skipped (symlink or junction): {path.name}")
                    continue
                if not path.is_file():
                    warnings.append(f"Unsupported memory entry {path.name}: only direct .md files can be registered.")
                    continue
                try:
                    name = _validate_name(path.name)
                except ValueError:
                    warnings.append(f"Unsupported memory file {path.name}: use a valid .md filename.")
                    continue
                relative = f".ai/{name}"
                if relative.casefold() not in known:
                    entries.append({"path": relative, "enabled": True})
                    known.add(relative.casefold())
                    added.append(relative)
            if added or not self.registry_path.exists():
                self._write_registry(entries, lease)
            return MemoryRenewal(tuple(added), tuple(warnings), tuple(missing))
