from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sys
import threading
import time

import psutil
import pytest

import discdock.processes as process_module
from discdock.processes import ProcessRunner


async def _wait_until_stopped(processes: list[psutil.Process], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(not process.is_running() for process in processes):
            return
        await asyncio.sleep(0.05)
    running = [process.pid for process in processes if process.is_running()]
    pytest.fail(f"test processes were still running after cancellation: {running}")


def _force_stop_test_processes(processes: list[psutil.Process]) -> None:
    for process in reversed(processes):
        try:
            if process.is_running():
                process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior is Windows-specific")
async def test_cancel_terminates_the_owned_process_tree() -> None:
    runner = ProcessRunner()
    owner = "process-tree-test"
    child_reported = asyncio.Event()
    grandchild_pid: int | None = None
    processes: list[psutil.Process] = []

    # Delay the grandchild spawn so ProcessRunner has time to assign the parent to
    # its Job Object. The grandchild should then inherit membership in that job.
    parent_program = (
        "import subprocess, sys, time\n"
        "time.sleep(0.75)\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(120)'],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        ")\n"
        "print(f'GRANDCHILD:{child.pid}', flush=True)\n"
        "time.sleep(120)\n"
    )

    async def on_line(line: str) -> None:
        nonlocal grandchild_pid
        if line.startswith("GRANDCHILD:"):
            grandchild_pid = int(line.split(":", 1)[1])
            child_reported.set()

    task = asyncio.create_task(
        runner.run(
            owner,
            [sys.executable, "-c", parent_program],
            timeout=60,
            no_output_timeout=30,
            on_line=on_line,
            keep_awake=False,
        )
    )
    try:
        await asyncio.wait_for(child_reported.wait(), timeout=15)
        parent_pid = runner.active_pid(owner)
        assert parent_pid is not None
        assert grandchild_pid is not None
        processes = [psutil.Process(parent_pid), psutil.Process(grandchild_pid)]
        assert all(process.is_running() for process in processes)

        assert await runner.cancel(owner) is True
        result = await asyncio.wait_for(task, timeout=20)

        assert result.cancelled is True
        assert runner.active_pid(owner) is None
        await _wait_until_stopped(processes)
        assert await runner.cancel(owner) is False
    finally:
        if not task.done():
            await runner.cancel(owner)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        _force_stop_test_processes(processes)


@pytest.mark.asyncio
async def test_cancel_unknown_owner_is_a_safe_noop() -> None:
    runner = ProcessRunner()
    assert await runner.cancel("not-running") is False
    assert runner.active_pids() == set()


@pytest.mark.asyncio
async def test_short_process_output_is_drained_through_eof() -> None:
    runner = ProcessRunner()
    captured: list[str] = []
    program = "print('FIRST', flush=True); print('FINAL_TITLE_DETAIL', flush=True)"

    result = await runner.run(
        "short-output-test",
        [sys.executable, "-c", program],
        timeout=10,
        no_output_timeout=5,
        on_line=lambda line: captured.append(line),
        keep_awake=False,
    )

    assert result.return_code == 0
    assert result.lines == ["FIRST", "FINAL_TITLE_DETAIL"]
    assert captured == result.lines


@pytest.mark.asyncio
async def test_a_flood_of_output_lines_does_not_hold_up_the_event_loop() -> None:
    runner = ProcessRunner()
    # FFmpeg decoding a damaged movie prints a line for every broken picture.
    program = "import sys\nfor index in range(100000):\n    sys.stdout.write(f'line {index}\\n')\nprint('LAST', flush=True)"
    seen = 0
    delays: list[float] = []
    done = asyncio.Event()

    def count(line: str) -> None:
        nonlocal seen
        seen += 1

    async def heartbeat() -> None:
        while not done.is_set():
            before = time.monotonic()
            await asyncio.sleep(0.01)
            delays.append(time.monotonic() - before)

    beat = asyncio.create_task(heartbeat())
    started = time.monotonic()
    result = await runner.run(
        "flood-test", [sys.executable, "-c", program], timeout=120, no_output_timeout=60, on_line=count, keep_awake=False
    )
    elapsed = time.monotonic() - started
    done.set()
    await beat

    assert result.return_code == 0 and seen == 100001 and result.lines[-1] == "LAST"
    assert max(delays) < 0.5, "the dashboard's requests still get their turn"
    assert elapsed < 30, "lines are handed over in batches, not one thread hop each"


def test_external_spawn_restores_frozen_dll_directory_on_failure(monkeypatch) -> None:
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(process_module, "_is_frozen_windows_runtime", lambda: True)
    monkeypatch.setattr(process_module, "_windows_dll_directory", lambda: r"C:\bundle")
    monkeypatch.setattr(
        process_module,
        "_set_windows_dll_directory",
        lambda value: events.append(("dll", value)),
    )

    def fail_to_start(args, **kwargs):
        events.append(("spawn", list(args)))
        raise OSError("test launch failure")

    monkeypatch.setattr(process_module.subprocess, "Popen", fail_to_start)
    with pytest.raises(OSError, match="test launch failure"):
        process_module.start_external_process(["tool.exe"], env={})

    assert events == [
        ("dll", None),
        ("spawn", ["tool.exe"]),
        ("dll", r"C:\bundle"),
    ]


def test_external_spawns_serialize_the_process_global_dll_window(monkeypatch) -> None:
    state_lock = threading.Lock()
    active_windows = 0
    maximum_active_windows = 0

    monkeypatch.setattr(process_module, "_is_frozen_windows_runtime", lambda: True)
    monkeypatch.setattr(process_module, "_windows_dll_directory", lambda: r"C:\bundle")

    def record_dll_directory(value: str | None) -> None:
        nonlocal active_windows, maximum_active_windows
        with state_lock:
            if value is None:
                active_windows += 1
                maximum_active_windows = max(maximum_active_windows, active_windows)
            else:
                active_windows -= 1

    def slow_start(args, **kwargs):
        time.sleep(0.05)
        return object()

    monkeypatch.setattr(process_module, "_set_windows_dll_directory", record_dll_directory)
    monkeypatch.setattr(process_module.subprocess, "Popen", slow_start)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(process_module.start_external_process, [f"tool-{index}.exe"], env={})
            for index in range(2)
        ]
        for future in futures:
            future.result(timeout=2)

    assert maximum_active_windows == 1
    assert active_windows == 0
