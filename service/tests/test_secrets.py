from __future__ import annotations

from pathlib import Path

from discdock.secrets import SecretStore


def test_decrypted_secrets_are_cached_and_returned_as_copies(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "secrets.bin"
    path.write_bytes(b"encrypted")
    store = SecretStore(path)
    calls = 0

    def unprotect(data: bytes) -> bytes:
        nonlocal calls
        assert data == b"encrypted"
        calls += 1
        return b'{"omdb_api_key":"secret"}'

    monkeypatch.setattr(store, "_unprotect", unprotect)

    first = store.all()
    first["omdb_api_key"] = "changed"

    assert store.all() == {"omdb_api_key": "secret"}
    assert calls == 1
