from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


class SecretStore:
    """Small DPAPI-protected secret store bound to the signed-in Windows user."""

    def __init__(self, path: Path):
        self.path = path
        self._cache: dict[str, str] | None = None

    @staticmethod
    def _protect(data: bytes) -> bytes:
        if os.name != "nt":
            raise RuntimeError("DPAPI is only available on Windows")
        import win32crypt

        protected = win32crypt.CryptProtectData(data, "DiscDock secrets", None, None, None, 0)
        return protected if isinstance(protected, bytes) else protected[1]

    @staticmethod
    def _unprotect(data: bytes) -> bytes:
        if os.name != "nt":
            raise RuntimeError("DPAPI is only available on Windows")
        import win32crypt

        unprotected = win32crypt.CryptUnprotectData(data, None, None, None, 0)
        return unprotected if isinstance(unprotected, bytes) else unprotected[1]

    def all(self) -> dict[str, str]:
        if self._cache is not None:
            return dict(self._cache)
        if not self.path.exists():
            self._cache = {}
            return {}
        try:
            values = json.loads(self._unprotect(self.path.read_bytes()).decode("utf-8"))
        except Exception as error:
            backup = self.path.with_suffix(".bin.bak")
            try:
                values = json.loads(self._unprotect(backup.read_bytes()).decode("utf-8"))
            except Exception:
                raise RuntimeError(
                    "The encrypted DiscDock secret store is damaged; it was not overwritten"
                ) from error
        self._cache = values
        return dict(values)

    def get(self, name: str, default: str = "") -> str:
        return self.all().get(name, default)

    def configured_names(self) -> set[str]:
        return {name for name, value in self.all().items() if value}

    def update(self, values: dict[str, str]) -> None:
        current = self.all()
        for name, value in values.items():
            if value:
                current[name] = value
            else:
                current.pop(name, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".bin.new")
        temporary.write_bytes(self._protect(json.dumps(current).encode("utf-8")))
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(".bin.bak"))
        os.replace(temporary, self.path)
        self._cache = dict(current)
