from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
_EXTERNAL_SPAWN_LOCK = threading.RLock()


def _is_frozen_windows_runtime() -> bool:
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def _windows_dll_directory() -> str | None:
    """Return the process DLL directory set by SetDllDirectoryW, if any."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetDllDirectoryW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p]
    kernel32.GetDllDirectoryW.restype = ctypes.c_uint32
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    ctypes.set_last_error(0)
    length = kernel32.GetDllDirectoryW(size, buffer)
    if length == 0:
        error = ctypes.get_last_error()
        if error:
            raise ctypes.WinError(error)
        return None
    if length >= size:
        buffer = ctypes.create_unicode_buffer(length + 1)
        length = kernel32.GetDllDirectoryW(len(buffer), buffer)
        if length == 0 or length >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


def _set_windows_dll_directory(value: str | None) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
    kernel32.SetDllDirectoryW.restype = ctypes.c_int
    if not kernel32.SetDllDirectoryW(value):
        raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def _external_spawn_boundary() -> Iterator[None]:
    """Temporarily remove PyInstaller's DLL directory while creating a child.

    SetDllDirectoryW changes process-global state. The lock prevents another
    thread from creating a child during the short sanitized window, and the
    original frozen-app directory is restored as soon as CreateProcess returns.
    """
    if not _is_frozen_windows_runtime():
        yield
        return
    with _EXTERNAL_SPAWN_LOCK:
        original = _windows_dll_directory()
        _set_windows_dll_directory(None)
        try:
            yield
        finally:
            _set_windows_dll_directory(original)


def external_process_environment() -> dict[str, str]:
    """Return an environment without PyInstaller's private binary directory."""
    environment = os.environ.copy()
    if _is_frozen_windows_runtime():
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
        clean_path: list[str] = []
        for item in environment.get("PATH", "").split(os.pathsep):
            try:
                if bundle == Path(item).resolve() or bundle in Path(item).resolve().parents:
                    continue
            except OSError:
                pass
            clean_path.append(item)
        environment["PATH"] = os.pathsep.join(clean_path)
    return environment


def start_external_process(args: list[str], **kwargs: Any) -> subprocess.Popen:
    """Create a third-party process without leaking its DLL policy to DiscDock."""
    if "env" not in kwargs:
        kwargs["env"] = external_process_environment()
    with _external_spawn_boundary():
        return subprocess.Popen(args, **kwargs)


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsJobObject:
    def __init__(self):
        self.handle = None
        self._kernel32 = None
        if os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        self._kernel32 = kernel32
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            self.handle, JobObjectExtendedLimitInformation, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(self.handle)
            self.handle = None
            raise ctypes.WinError(error)

    def assign(self, process: subprocess.Popen) -> None:
        if (
            self.handle
            and os.name == "nt"
            and not self._kernel32.AssignProcessToJobObject(self.handle, int(process._handle))
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle and os.name == "nt":
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


class CaptureCancelled(RuntimeError):
    """A helper process started with run_capture() was stopped because its job was cancelled."""


@dataclass
class _Capture:
    process: subprocess.Popen
    cancelled: bool = False


_CAPTURES: dict[str, list[_Capture]] = {}
_CANCELLED_CAPTURE_OWNERS: set[str] = set()
_CAPTURE_LOCK = threading.Lock()


def begin_captures(owner: str) -> None:
    """Let run_capture() start processes for ``owner`` again after an earlier cancellation."""
    with _CAPTURE_LOCK:
        _CANCELLED_CAPTURE_OWNERS.discard(owner)


def cancel_captures(owner: str | None = None) -> int:
    """Stop the captured processes of one owner, or of every owner.

    Until begin_captures() is called for that owner again, new captures for it
    refuse to start, so a multi-step scan cannot continue with its next step.
    """
    with _CAPTURE_LOCK:
        owners = list(_CAPTURES) if owner is None else [owner]
        if owner is not None:
            _CANCELLED_CAPTURE_OWNERS.add(owner)
        captures = [capture for name in owners for capture in _CAPTURES.get(name, [])]
        for capture in captures:
            capture.cancelled = True
    for capture in captures:
        try:
            capture.process.kill()
        except OSError:
            pass
    return len(captures)


def run_capture(
    args: list[str], *, timeout: float, owner: str = "", env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run a helper such as FFprobe to completion and return its exit code, stdout and stderr.

    The process belongs to a Windows job object, so it ends with DiscDock, and
    cancel_captures() stops it when its job is cancelled. ``env`` adds variables
    to its environment.
    """
    with _CAPTURE_LOCK:
        if owner and owner in _CANCELLED_CAPTURE_OWNERS:
            raise CaptureCancelled("The job was cancelled")
    process = start_external_process(
        args,
        env={**external_process_environment(), **(env or {})},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    job: WindowsJobObject | None = None
    try:
        job = WindowsJobObject()
        job.assign(process)
    except OSError:
        pass
    capture = _Capture(process)
    with _CAPTURE_LOCK:
        _CAPTURES.setdefault(owner, []).append(capture)
        if owner and owner in _CANCELLED_CAPTURE_OWNERS:
            capture.cancelled = True
    if capture.cancelled:
        process.kill()
    timed_out = False
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            stdout, stderr = process.communicate()
    finally:
        with _CAPTURE_LOCK:
            entries = _CAPTURES.get(owner, [])
            if capture in entries:
                entries.remove(capture)
            if not entries:
                _CAPTURES.pop(owner, None)
        if job is not None:
            job.close()
    if capture.cancelled:
        raise CaptureCancelled("The job was cancelled")
    return (-1 if timed_out else process.returncode), stdout, stderr


@dataclass
class ProcessResult:
    args: list[str]
    return_code: int
    lines: list[str] = field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False
    duration_seconds: float = 0


LineCallback = Callable[[str], Awaitable[None] | None]


class ProcessFailure(RuntimeError):
    def __init__(self, message: str, result: ProcessResult):
        super().__init__(message)
        self.result = result


class _OutputPump:
    """Reads a child's output in its own thread and hands the lines to the event loop in batches.

    Waiting for every line separately costs the event loop a thread hop per line.
    A tool that prints thousands of lines a second, like FFmpeg decoding a damaged
    movie, then keeps the event loop so busy that the dashboard stops answering.
    """

    def __init__(self, stream: Any, loop: asyncio.AbstractEventLoop):
        self._lock = threading.Lock()
        self._lines: list[str] = []
        self._finished = False
        self._notified = False
        self._ready = asyncio.Event()
        self._loop = loop
        self._thread = threading.Thread(target=self._read, args=(stream,), name="process-output", daemon=True)
        self._thread.start()

    def _read(self, stream: Any) -> None:
        try:
            for line in iter(stream.readline, ""):
                self._add(line)
        except (OSError, ValueError):
            pass
        finally:
            self._add(None)

    def _add(self, line: str | None) -> None:
        with self._lock:
            if line is None:
                self._finished = True
            else:
                self._lines.append(line)
            if self._notified:
                return
            self._notified = True
        try:
            self._loop.call_soon_threadsafe(self._ready.set)
        except RuntimeError:
            # The event loop has already closed.
            pass

    async def wait(self, timeout: float) -> bool:
        """Whether output or the end of output arrived within ``timeout`` seconds."""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=max(0.0, timeout))
        except TimeoutError:
            return False
        return True

    def take(self) -> tuple[list[str], bool]:
        """The lines read so far, and whether the output has ended."""
        with self._lock:
            lines, self._lines = self._lines, []
            finished = self._finished
            self._notified = False
            self._ready.clear()
        return lines, finished

    def join(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()


class ProcessRunner:
    def __init__(self):
        self._running: dict[str, tuple[subprocess.Popen, WindowsJobObject]] = {}
        self._lock = asyncio.Lock()
        self._cancel_requested: set[str] = set()

    @staticmethod
    def _set_awake(enabled: bool) -> None:
        if os.name == "nt":
            flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if enabled else 0)
            ctypes.WinDLL("kernel32").SetThreadExecutionState(flags)

    async def run(
        self,
        owner_id: str,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: float,
        no_output_timeout: float,
        on_line: LineCallback | None = None,
        keep_awake: bool = True,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        """Run a tool; ``env`` adds variables to its environment."""
        if not args or not Path(args[0]).is_file():
            raise FileNotFoundError(args[0] if args else "executable")
        started = time.monotonic()
        async with self._lock:
            if owner_id in self._running:
                raise RuntimeError(f"A process is already running for {owner_id}")
        process = start_external_process(
            args,
            env={**external_process_environment(), **(env or {})},
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            job = WindowsJobObject()
            job.assign(process)
        except Exception:
            process.kill()
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
            raise
        async with self._lock:
            if owner_id in self._running:
                job.close()
                process.kill()
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
                raise RuntimeError(f"A process is already running for {owner_id}")
            self._running[owner_id] = (process, job)
            self._cancel_requested.discard(owner_id)
        if keep_awake:
            self._set_awake(True)
        lines: deque[str] = deque(maxlen=2000)
        timed_out = False
        cancelled = False
        output: _OutputPump | None = None

        async def accept_line(line: str) -> None:
            clean = line.rstrip("\r\n")
            lines.append(clean)
            if on_line:
                response = on_line(clean)
                if asyncio.iscoroutine(response):
                    await response

        try:
            # The pump reads through EOF instead of stopping as soon as the child
            # exits. Fast CLI tools can exit while Windows still has their last
            # buffered lines waiting in the pipe; those final MakeMKV TINFO
            # records contain the selectable title list.
            output = _OutputPump(process.stdout, asyncio.get_running_loop())
            last_output = time.monotonic()
            while True:
                now = time.monotonic()
                if now - started > timeout:
                    timed_out = True
                    await self.cancel(owner_id)
                    break
                arrived = await output.wait(min(no_output_timeout - (now - last_output), timeout - (now - started)))
                batch, finished = output.take()
                if batch:
                    last_output = time.monotonic()
                for count, line in enumerate(batch, start=1):
                    await accept_line(line)
                    if count % 500 == 0:
                        # Let the dashboard's requests in between a flood of output.
                        await asyncio.sleep(0)
                if finished:
                    break
                if not arrived and time.monotonic() - last_output >= no_output_timeout:
                    timed_out = True
                    await self.cancel(owner_id)
                    break
            try:
                return_code = await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=10)
            except TimeoutError:
                await self._terminate(process, job)
                return_code = int(process.returncode if process.returncode is not None else -1)
            cancelled = (
                owner_id in self._cancel_requested
                or return_code < 0
                or (os.name == "nt" and return_code in {1, 255})
                and not timed_out
            )
            return ProcessResult(
                args=args,
                return_code=return_code,
                lines=list(lines),
                timed_out=timed_out,
                cancelled=cancelled,
                duration_seconds=time.monotonic() - started,
            )
        finally:
            if keep_awake:
                self._set_awake(False)
            async with self._lock:
                self._running.pop(owner_id, None)
                self._cancel_requested.discard(owner_id)
            job.close()
            # Closing a pipe that a reader is still blocked on would wait; the pipe goes with the process.
            if process.stdout and (output is None or await asyncio.to_thread(output.join, 5.0)):
                process.stdout.close()

    async def cancel(self, owner_id: str) -> bool:
        async with self._lock:
            running = self._running.get(owner_id)
            if running:
                self._cancel_requested.add(owner_id)
        if not running:
            return False
        process, job = running
        if process.poll() is not None:
            return True
        await self._terminate(process, job)
        return True

    async def _terminate(self, process: subprocess.Popen, job: WindowsJobObject) -> None:
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
        except Exception:
            try:
                job.close()
                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
            except Exception:
                process.kill()
                try:
                    await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5)
                except TimeoutError as error:
                    raise RuntimeError(f"Process {process.pid} did not terminate") from error

    def active_pid(self, owner_id: str) -> int | None:
        running = self._running.get(owner_id)
        return running[0].pid if running and running[0].poll() is None else None

    def active_pids(self) -> set[int]:
        return {process.pid for process, _ in self._running.values() if process.poll() is None}
