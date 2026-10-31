"""Workspace-contained file access, pinned across an approved write.

POSIX uses directory-relative descriptors and O_NOFOLLOW. Windows holds
non-reparse directory handles without write/delete sharing until the leaf
handle is verified and closed. Validation never creates filesystem entries.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path, PureWindowsPath
import re
import stat


def _identity(info):
    return info.st_dev, info.st_ino


def _plain(info, *, directory):
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Symlinks, junctions and reparse points are not edit targets.")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise ValueError("Expected a directory." if directory else "Expected a regular file.")
    if not directory and info.st_nlink != 1:
        # The writer modifies the reviewed inode in place. A second hard link
        # could otherwise make an approved edit change a file outside the root.
        raise ValueError("Hard-linked files cannot be edited safely inside one workspace.")


def _canonical_directory(path):
    """Reject reparse redirection anywhere in a registered canonical root."""
    requested = Path(path).absolute()
    info = requested.lstat()
    _plain(info, directory=True)
    if requested.resolve(strict=True) != requested:
        raise ValueError("The registered workspace root was redirected by an ancestor path.")
    return requested, info


@dataclass(frozen=True)
class SafeTarget:
    root: Path
    path: Path
    parts: tuple[str, ...]
    workspace_id: int
    root_identity: tuple[int, int]
    parent_identities: tuple[tuple[tuple[str, ...], tuple[int, int]], ...]
    file_identity: tuple[int, int] | None


def prepare_target(workspace_manager, reference, *, selected_workspace_id=None, creating=False):
    raw = reference.strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1].strip()
    match = re.fullmatch(r"(\d+)/(.+)", raw)
    if match is None:
        raise ValueError("Expected a workspace-relative file reference: <id/path>.")
    workspace_id = int(match.group(1))
    relative = match.group(2)
    if creating and (selected_workspace_id is None or workspace_id != selected_workspace_id):
        raise ValueError("CREATE requires the explicitly selected target workspace.")
    if relative.startswith(("/", "\\")) or PureWindowsPath(relative).drive:
        raise ValueError("Absolute and drive-qualified edit paths are not allowed.")
    segments = relative.replace("\\", "/").split("/")
    if any(part == ".." for part in segments):
        raise ValueError("Parent traversal is not allowed in edit paths.")
    parts = tuple(part for part in segments if part != ".")
    if not parts or any(not part for part in parts):
        raise ValueError("Edit targets must identify a file.")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    for part in parts:
        if any(ord(c) < 32 or c in '<>:"|?*' for c in part) or part.endswith((" ", ".")):
            raise ValueError("Invalid filename component in edit path.")
        if part.split(".", 1)[0].upper() in reserved:
            raise ValueError("Reserved device names cannot be edit targets.")
    entry = next((e for e in workspace_manager.list_directories() if e["id"] == workspace_id), None)
    if entry is None:
        raise ValueError(f"Workspace directory #{workspace_id} does not exist.")
    root, root_info = _canonical_directory(entry["path"])
    parents = []
    current = root
    for index, part in enumerate(parts[:-1]):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        _plain(info, directory=True)
        parents.append((parts[:index + 1], _identity(info)))
    path = root.joinpath(*parts)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not creating:
            raise FileNotFoundError(f"MODIFY target does not exist: {reference}")
        file_identity = None
    else:
        _plain(info, directory=False)
        if creating:
            raise FileExistsError(f"CREATE target already exists: {reference}")
        file_identity = _identity(info)
    return SafeTarget(root, path, parts, workspace_id, _identity(root_info), tuple(parents), file_identity)


if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL

    class _FileInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
                    ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
                    ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
                    ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
                    ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]

    _kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    _kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    _kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD

    def _win_open(path, *, directory=False, write=False, create=False):
        access = 0x80000000 if directory else (0xC0000000 if write and not create else 0x40000000 if write else 0x80000000)
        # Sharing read only pins every directory against write/reparse/delete.
        handle = _kernel.CreateFileW(str(path), access, 1 if directory else 0, None,
                                     1 if create else 3, 0x00200000 | (0x02000000 if directory else 0), None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = _FileInfo()
            if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & 0x400 or bool(info.attributes & 0x10) != directory:
                raise ValueError("Edit path changed to a reparse point or unexpected file type.")
            buffer = ctypes.create_unicode_buffer(32768)
            count = _kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            if not count or count >= len(buffer):
                raise OSError("Unable to verify the opened edit path.")
            final_path = buffer.value
            if final_path.startswith("\\\\?\\UNC\\"):
                final_path = "\\\\" + final_path[8:]
            elif final_path.startswith("\\\\?\\"):
                final_path = final_path[4:]
            if os.path.normcase(os.path.abspath(final_path)) != os.path.normcase(os.path.abspath(path)):
                raise ValueError("Edit path was redirected while opening it.")
            return handle
        except BaseException:
            _kernel.CloseHandle(handle)
            raise


@contextmanager
def _parent(target, *, create=False):
    _, root_info = _canonical_directory(target.root)
    if _identity(root_info) != target.root_identity:
        raise RuntimeError("The selected workspace changed after validation.")
    known = dict(target.parent_identities)
    if os.name == "nt":
        handles = []
        try:
            handles.append(_win_open(target.root, directory=True))
            if _identity(target.root.lstat()) != target.root_identity:
                raise RuntimeError("The selected workspace changed after validation.")
            current = target.root
            for index, part in enumerate(target.parts[:-1]):
                current = current / part
                if create:
                    try:
                        current.mkdir()
                    except FileExistsError:
                        pass
                handles.append(_win_open(current, directory=True))
                identity = known.get(target.parts[:index + 1])
                if identity is not None and _identity(current.lstat()) != identity:
                    raise RuntimeError("An edit parent directory changed after validation.")
            yield None
        finally:
            for handle in reversed(handles):
                _kernel.CloseHandle(handle)
    else:
        descriptors = []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            parent = os.open(target.root, flags)
            descriptors.append(parent)
            if _identity(os.fstat(parent)) != target.root_identity:
                raise RuntimeError("The selected workspace changed after validation.")
            for index, part in enumerate(target.parts[:-1]):
                try:
                    next_parent = os.open(part, flags, dir_fd=parent)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, dir_fd=parent)
                    except FileExistsError:
                        pass
                    next_parent = os.open(part, flags, dir_fd=parent)
                descriptors.append(next_parent)
                parent = next_parent
                identity = known.get(target.parts[:index + 1])
                if identity is not None and _identity(os.fstat(parent)) != identity:
                    raise RuntimeError("An edit parent directory changed after validation.")
            yield parent
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)


def _leaf(target, parent, *, write=False, create=False):
    if os.name == "nt":
        handle = _win_open(target.path, write=write, create=create)
        try:
            descriptor = msvcrt.open_osfhandle(int(handle), (os.O_RDWR if write and not create else os.O_WRONLY if write else os.O_RDONLY) | os.O_BINARY)
        except BaseException:
            _kernel.CloseHandle(handle)
            raise
    else:
        flags = (os.O_RDWR if write and not create else os.O_WRONLY if write else os.O_RDONLY) | os.O_NOFOLLOW
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        descriptor = os.open(target.parts[-1], flags, 0o666, dir_fd=parent)
    try:
        info = os.fstat(descriptor)
        _plain(info, directory=False)
        if not create and target.file_identity is not None and _identity(info) != target.file_identity:
            raise RuntimeError("The edit target was replaced after validation.")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def safe_read_bytes(target):
    with _parent(target) as parent:
        descriptor = _leaf(target, parent)
        with os.fdopen(descriptor, "rb") as stream:
            return stream.read()


def safe_write_bytes(target, content, *, expected_bytes=None, create=False):
    if not isinstance(content, bytes):
        raise TypeError("Reviewed file content must be bytes.")
    with _parent(target, create=create) as parent:
        descriptor = _leaf(target, parent, write=True, create=create)
        with os.fdopen(descriptor, "wb" if create else "r+b") as stream:
            if not create:
                if expected_bytes is None:
                    raise ValueError("MODIFY requires the reviewed original bytes.")
                if stream.read() != expected_bytes:
                    raise RuntimeError("File changed after its diff was reviewed; refusing to overwrite it.")
                stream.seek(0)
            _plain(os.fstat(stream.fileno()), directory=False)
            stream.write(content)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
