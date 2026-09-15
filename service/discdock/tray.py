"""DiscDock's icon in the Windows notification area, with Open dashboard, Restart and Stop."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

import win32api
import win32con
import win32gui

from . import __version__

logger = logging.getLogger(__name__)

OPEN_DASHBOARD, RESTART, STOP = 1001, 1002, 1003
NOTIFY = win32con.WM_USER + 20


def icon_path() -> Path:
    """The DiscDock logo as an icon: inside the installed program, or in assets when run from source."""
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "discdock.ico"
    if getattr(sys, "frozen", False) and bundled.is_file():
        return bundled
    return Path(__file__).resolve().parents[2] / "assets" / "discdock.ico"


def successor_command(pid: int) -> list[str]:
    """How to start the DiscDock that takes over once the one with this process id has stopped."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--background", "--after", str(pid)]
    return [sys.executable, "-m", "discdock", "--background", "--after", str(pid)]


def start_successor() -> None:
    command = successor_command(os.getpid())
    detached = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        # Outside any job object this DiscDock runs in, so its exit cannot end the new one.
        subprocess.Popen(command, creationflags=detached | subprocess.CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
    except OSError:
        subprocess.Popen(command, creationflags=detached, close_fds=True)


class TrayActions:
    """What the menu items do, apart from the Windows calls so it can be tested."""

    def __init__(
        self,
        *,
        busy_jobs: Callable[[], list[str]],
        ask: Callable[[str], bool],
        request_exit: Callable[[], None],
        start_successor: Callable[[], None],
        open_dashboard: Callable[[], None],
    ):
        self.busy_jobs = busy_jobs
        self.ask = ask
        self.request_exit = request_exit
        self.start_successor = start_successor
        self.open_dashboard = open_dashboard

    def open(self) -> None:
        self.open_dashboard()

    def stop(self) -> bool:
        if not self._may_interrupt("Stop"):
            return False
        self.request_exit()
        return True

    def restart(self) -> bool:
        if not self._may_interrupt("Restart"):
            return False
        self.start_successor()
        self.request_exit()
        return True

    def _may_interrupt(self, verb: str) -> bool:
        busy = self.busy_jobs()
        if not busy:
            return True
        return self.ask(
            f"DiscDock is working on {busy[0]}. If you {verb.lower()} it now, that job stops, and you can "
            f"retry it afterwards.\n\n{verb} DiscDock anyway?"
        )


class TrayIcon:
    def __init__(self, tooltip: str):
        self.tooltip = tooltip[:127]
        self.actions: TrayActions | None = None
        self._hwnd = 0
        self._icon = 0
        self._taskbar_created = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="notification-area-icon", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(5)

    def stop(self) -> None:
        """Removes the icon; called while DiscDock shuts down."""
        if self._hwnd:
            win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
            self._thread.join(3)

    def ask(self, question: str) -> bool:
        style = win32con.MB_YESNO | win32con.MB_ICONWARNING | win32con.MB_SETFOREGROUND
        return win32api.MessageBox(self._hwnd, question, "DiscDock", style) == win32con.IDYES

    def _run(self) -> None:
        try:
            instance = win32api.GetModuleHandle(None)
            self._taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
            window_class = win32gui.WNDCLASS()
            window_class.hInstance = instance
            window_class.lpszClassName = "DiscDockNotificationArea"
            window_class.lpfnWndProc = {
                self._taskbar_created: self._on_taskbar_created,
                NOTIFY: self._on_notify,
                win32con.WM_COMMAND: self._on_command,
                win32con.WM_CLOSE: self._on_close,
                win32con.WM_DESTROY: self._on_destroy,
            }
            atom = win32gui.RegisterClass(window_class)
            self._hwnd = win32gui.CreateWindow(atom, "DiscDock", 0, 0, 0, 0, 0, 0, 0, instance, None)
            self._icon = self._load_icon()
            self._show(win32gui.NIM_ADD)
            self._ready.set()
            win32gui.PumpMessages()
        except Exception as error:
            logger.warning("DiscDock's icon could not be shown in the notification area: %s", error)
        finally:
            self._ready.set()

    @staticmethod
    def _load_icon() -> int:
        size = win32api.GetSystemMetrics(win32con.SM_CXSMICON)
        try:
            return win32gui.LoadImage(0, str(icon_path()), win32con.IMAGE_ICON, size, size, win32con.LR_LOADFROMFILE)
        except Exception as error:
            logger.warning("The DiscDock icon could not be loaded (%s); using the standard program icon", error)
            return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    def _show(self, message: int) -> None:
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        win32gui.Shell_NotifyIcon(message, (self._hwnd, 0, flags, NOTIFY, self._icon, self.tooltip))

    def _on_taskbar_created(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        # Explorer restarted, and its notification area starts out empty.
        self._show(win32gui.NIM_ADD)
        return 0

    def _on_notify(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if lparam == win32con.WM_LBUTTONUP and self.actions:
            self.actions.open()
        elif lparam in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
            self._show_menu()
        return 0

    def _show_menu(self) -> None:
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING | win32con.MF_GRAYED, 0, f"DiscDock {__version__}")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, OPEN_DASHBOARD, "Open dashboard")
        win32gui.AppendMenu(menu, win32con.MF_STRING, RESTART, "Restart DiscDock")
        win32gui.AppendMenu(menu, win32con.MF_STRING, STOP, "Stop DiscDock")
        win32gui.SetMenuDefaultItem(menu, OPEN_DASHBOARD, False)
        x, y = win32gui.GetCursorPos()
        try:
            # Only a menu of the foreground window closes when you click somewhere else.
            win32gui.SetForegroundWindow(self._hwnd)
        except Exception:
            pass
        win32gui.TrackPopupMenu(menu, win32con.TPM_RIGHTBUTTON | win32con.TPM_BOTTOMALIGN, x, y, 0, self._hwnd, None)
        win32gui.PostMessage(self._hwnd, win32con.WM_NULL, 0, 0)
        win32gui.DestroyMenu(menu)

    def _on_command(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        item = win32api.LOWORD(wparam)
        if self.actions and item == OPEN_DASHBOARD:
            self.actions.open()
        elif self.actions and item == RESTART:
            self.actions.restart()
        elif self.actions and item == STOP:
            self.actions.stop()
        return 0

    def _on_close(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self._hwnd, 0))
        win32gui.DestroyWindow(hwnd)
        return 0

    def _on_destroy(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        win32gui.PostQuitMessage(0)
        return 0


def start_tray_icon(
    *, busy_jobs: Callable[[], list[str]], request_exit: Callable[[], None], dashboard_url: str
) -> TrayIcon | None:
    """Shows DiscDock in the notification area; None when it cannot be shown."""
    if sys.platform != "win32":
        return None
    icon = TrayIcon(f"DiscDock {__version__}")
    icon.actions = TrayActions(
        busy_jobs=busy_jobs,
        ask=icon.ask,
        request_exit=request_exit,
        start_successor=start_successor,
        open_dashboard=lambda: webbrowser.open(dashboard_url),
    )
    icon.start()
    return icon
