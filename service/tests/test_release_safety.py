from __future__ import annotations

import logging

from discdock.main import _SecretRedactionFilter
from discdock.models import JobPatch
from discdock.settings import _winget_portable


def test_logs_redact_query_keys_and_bearer_tokens() -> None:
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "GET https://example.invalid/?apikey=private-value Authorization: Bearer another-secret",
        (),
        None,
    )

    assert _SecretRedactionFilter().filter(record)
    rendered = record.getMessage()
    assert "private-value" not in rendered
    assert "another-secret" not in rendered
    assert rendered.count("[redacted]") == 2


def test_winget_portable_discovery(monkeypatch, tmp_path) -> None:
    executable = (
        tmp_path
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "Example.Tool_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "tool.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert _winget_portable("Example.Tool", "tool.exe") == str(executable.resolve())


def test_manual_metadata_patch_is_typed() -> None:
    patch = JobPatch.model_validate(
        {
            "metadata": {
                "provider": "omdb",
                "provider_id": "tt1234567",
                "title": "Example",
                "year": "2026",
                "media_kind": "movie",
                "poster_url": "https://example.invalid/poster.jpg",
                "plot": "A test.",
            }
        }
    )

    assert patch.metadata is not None
    assert patch.metadata.provider_id == "tt1234567"


def test_ffmpeg_from_a_winget_package_is_found_before_it_reaches_path(monkeypatch, tmp_path) -> None:
    from discdock import settings

    executable = (
        tmp_path
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "ffmpeg-7.1-full_build"
        / "bin"
        / "ffprobe.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(settings.shutil, "which", lambda name: None)

    assert settings._ffmpeg_tool("ffprobe.exe") == str(executable.resolve())
