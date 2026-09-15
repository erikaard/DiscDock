from __future__ import annotations

import re
from pathlib import Path

from .secrets import SecretStore

ARM_SECRET_NAMES = {
    "OMDB_API_KEY": "omdb_api_key",
    "TMDB_API_KEY": "tmdb_api_key",
    "ARM_API_KEY": "arm_api_key",
}


def import_arm_secrets(config_path: Path, secret_store: SecretStore) -> set[str]:
    """Import supported ARM secrets without retaining or logging plaintext."""
    if not config_path.is_file():
        raise FileNotFoundError(config_path)
    imported: dict[str, str] = {}
    for line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        target = ARM_SECRET_NAMES.get(key.strip())
        if not target:
            continue
        value = re.sub(r"\s+#.*$", "", raw_value).strip().strip("\"'")
        if value:
            imported[target] = value
    if imported:
        secret_store.update(imported)
    return set(imported)
