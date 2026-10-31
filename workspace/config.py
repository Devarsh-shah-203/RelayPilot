"""Persistent workspace registration, including first-run and legacy migration."""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


class WorkspaceConfig:
    CONFIG_DIR_NAME = ".relaypilot"
    CONFIG_FILE_NAME = "workspace.json"

    def __init__(self, gateway_dir):
        self.gateway_dir = Path(gateway_dir).resolve()
        self.config_dir = self.gateway_dir / self.CONFIG_DIR_NAME
        self.config_file = self.config_dir / self.CONFIG_FILE_NAME

    def _check_paths(self):
        for path in (self.config_dir, self.config_file):
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("Workspace configuration cannot use a symlink or junction.")

    def ensure_config(self):
        self._check_paths()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if not self.config_file.exists():
            legacy = self.gateway_dir / ".ai-gateway" / self.CONFIG_FILE_NAME
            if legacy.is_file():
                payload = legacy.read_text(encoding="utf-8")
                json.loads(payload)
            else:
                payload = json.dumps({"directories": []}, indent=4)
            try:
                with self.config_file.open("x", encoding="utf-8") as handle:
                    handle.write(payload)
            except FileExistsError:
                self._check_paths()

    def load(self):
        self.ensure_config()
        try:
            data = json.loads(self.config_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError("Workspace configuration contains invalid JSON.") from error
        if not isinstance(data, dict) or not isinstance(data.get("directories"), list):
            raise ValueError("Workspace configuration must contain a directories list.")
        validated = []
        used = set()
        for entry in data["directories"]:
            if not isinstance(entry, dict):
                raise ValueError("Each workspace entry must be an object.")
            wid, path = entry.get("id"), entry.get("path")
            if type(wid) is not int or wid <= 0 or wid in used:
                raise ValueError("Workspace IDs must be distinct positive integers.")
            if not isinstance(path, str) or not path.strip():
                raise ValueError("Workspace path must be a non-empty string.")
            used.add(wid)
            validated.append({"id": wid, "path": path})
        return validated

    def save(self, directories):
        if not isinstance(directories, list):
            raise TypeError("directories must be a list.")
        self._check_paths()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.config_dir,
                                    prefix="workspace-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({"directories": directories}, handle, indent=4)
                handle.flush()
                os.fsync(handle.fileno())
            self._check_paths()
            os.replace(temporary, self.config_file)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
