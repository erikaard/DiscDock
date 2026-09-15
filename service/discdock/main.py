from __future__ import annotations

import argparse
import multiprocessing
import os
import re
import socket
import sys
import threading
import time
import webbrowser
from logging import INFO, WARNING, Filter, Formatter, StreamHandler, getLogger
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil
import uvicorn

from .settings import SettingsStore, default_data_root


class _SecretRedactionFilter(Filter):
    _query_secret = re.compile(r"(?i)((?:api_?key|token|password|secret)=)[^&\s\"']+")
    _bearer = re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s\"']+")

    def filter(self, record) -> bool:
        message = record.getMessage()
        message = self._query_secret.sub(r"\1[redacted]", message)
        message = self._bearer.sub(r"\1[redacted]", message)
        record.msg = message
        record.args = ()
        return True


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="DiscDock", description="Windows-native automatic disc ripping")
    parser.add_argument("--open-browser", action="store_true", help="Open the dashboard after startup")
    parser.add_argument("--background", action="store_true", help="Run without opening the dashboard")
    # Restart in the notification area starts the new DiscDock before the old one has stopped.
    parser.add_argument("--after", type=int, metavar="PID", help=argparse.SUPPRESS)
    parser.add_argument(
        "--import-arm-config",
        type=str,
        metavar="PATH",
        help="Securely import API keys from an ARM arm.yaml file",
    )
    return parser.parse_args()


def _service_is_ready(host: str, port: int) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/v1/health", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def _wait_for_exit(pid: int, timeout: float = 60) -> None:
    try:
        psutil.Process(pid).wait(timeout)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        pass


def _configure_logging(log_directory) -> None:
    log_directory.mkdir(parents=True, exist_ok=True)
    root = getLogger()
    root.setLevel(INFO)
    formatter = Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        log_directory / "discdock.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_SecretRedactionFilter())
    root.handlers.clear()
    root.addHandler(file_handler)
    # HTTP client request URLs may contain API credentials. Keep dependency
    # request logging out of both the console and persistent application log.
    getLogger("httpx").setLevel(WARNING)
    getLogger("httpcore").setLevel(WARNING)
    if sys.stdout is not None:
        console = StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        console.addFilter(_SecretRedactionFilter())
        root.addHandler(console)


def _open_when_ready(host: str, port: int) -> None:
    for _ in range(100):
        try:
            with socket.create_connection((host, port), timeout=0.25):
                webbrowser.open(f"http://{host}:{port}/")
                return
        except OSError:
            time.sleep(0.1)


def run() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--disc-rescue":
        # The packaged service starts its own executable as the rescue helper,
        # so a stuck drive read can be stopped without stopping DiscDock.
        from .disc_rescue import main as rescue_main

        raise SystemExit(rescue_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--bluray-decrypt":
        # MakeMKV's library decrypts a rescued Blu-ray movie in a separate process,
        # so a fault inside the library cannot stop DiscDock.
        from .bluray_decrypt import main as decrypt_main

        raise SystemExit(decrypt_main(sys.argv[2:]))
    multiprocessing.freeze_support()
    arguments = _arguments()
    store = SettingsStore(default_data_root() / "config" / "settings.json")
    settings = store.load()
    _configure_logging(settings.resolved_directories()["logs"])
    if arguments.import_arm_config:
        from .migration import import_arm_secrets
        from .secrets import SecretStore

        secret_store = SecretStore(default_data_root() / "config" / "secrets.bin")
        imported = import_arm_secrets(Path(arguments.import_arm_config).expanduser().resolve(), secret_store)
        getLogger(__name__).info("Imported %d encrypted setting(s) from ARM", len(imported))
        return
    if arguments.after:
        _wait_for_exit(arguments.after)
    wants_browser = arguments.open_browser or os.environ.get("DISCDOCK_OPEN_BROWSER") == "1"
    if wants_browser and _service_is_ready(settings.host, settings.port):
        webbrowser.open(f"http://{settings.host}:{settings.port}/")
        return
    if wants_browser and not arguments.background:
        threading.Thread(
            target=_open_when_ready, args=(settings.host, settings.port), daemon=True, name="dashboard-opener"
        ).start()
    from .api import app

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    run()
