"""Launch the packaged browser workspace on the local machine."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Mapping
from pathlib import Path
from secrets import token_urlsafe
from typing import Any


def default_workspace_data_dir(
    *,
    platform_name: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the conventional per-user application data directory."""
    platform_name = sys.platform if platform_name is None else platform_name
    environment = os.environ if environment is None else environment
    home = Path.home() if home is None else home

    if platform_name.startswith("win"):
        root = environment.get("LOCALAPPDATA") or environment.get("APPDATA")
        base = Path(root).expanduser() if root else home / "AppData" / "Local"
    elif platform_name == "darwin":
        base = home / "Library" / "Application Support"
    else:
        root = environment.get("XDG_DATA_HOME")
        base = Path(root).expanduser() if root else home / ".local" / "share"
    return base / "powdrr-lift"


def run_workspace_ui(args: argparse.Namespace) -> int:
    """Start the workspace API and bundled UI until the user stops the process."""
    import powdrr_local
    import uvicorn

    from powdrr_lift.workrr.workspace_app import build_workspace_app

    static_directory = Path(powdrr_local.__file__).parent / "web_assets"
    if not (static_directory / "index.html").is_file():
        print(
            "Workspace UI assets are missing. Rebuild the package after running "
            "npm run build in ui/.",
            file=sys.stderr,
        )
        return 2

    data_directory = (
        args.data_dir.expanduser()
        if args.data_dir is not None
        else default_workspace_data_dir()
    ).resolve()
    try:
        data_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        print(f"Cannot create workspace data directory: {error}", file=sys.stderr)
        return 2

    try:
        instance_id = _load_or_create_instance_id(data_directory)
    except OSError as error:
        print(f"Cannot identify workspace instance: {error}", file=sys.stderr)
        return 2

    host = "127.0.0.1"
    origin = f"http://{host}:{args.port}"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind((host, args.port))
        listener.listen(socket.SOMAXCONN)
    except OSError as error:
        listener.close()
        if _workspace_instance_is_running(origin, instance_id):
            print(f"Reusing the running workspace for {data_directory}: {origin}")
            if args.no_open:
                print(
                    "Open the workspace URL in your browser and use "
                    "its existing session."
                )
            elif not webbrowser.open_new_tab(origin):
                print(f"Open the workspace in your browser: {origin}")
            return 0
        print(
            f"Cannot use workspace port {args.port}: {error}. "
            "The service on that port is not the workspace for this data directory. "
            "Choose another port with --port.",
            file=sys.stderr,
        )
        return 2

    bootstrap_secret = token_urlsafe(32)
    app = build_workspace_app(
        data_dir=data_directory,
        bootstrap_secret=bootstrap_secret,
        expected_origin=origin,
        repository_root=args.repo_root,
        static_directory=static_directory,
        workspace_instance_id=instance_id,
    )
    print(f"Workspace: {origin}")
    print(f"One-use workspace token: {bootstrap_secret}")
    print(f"Workspace data: {data_directory}")
    if args.repo_root is not None:
        print(f"Registered repository: {args.repo_root.expanduser().resolve()}")
    if args.no_open:
        print("Open the workspace URL in your browser and enter the one-use token.")
    else:
        threading.Thread(
            target=_open_when_healthy,
            args=(origin,),
            name="workspace-browser-launch",
            daemon=True,
        ).start()

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=args.port, log_level="info")
    )
    try:
        server.run(sockets=[listener])
    except KeyboardInterrupt:
        server.should_exit = True
    finally:
        listener.close()
    return 0


def _load_or_create_instance_id(data_directory: Path) -> str:
    """Persist a stable identity for the workspace backed by this data directory."""
    marker = data_directory / ".workspace-instance-id"
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        instance_id = token_urlsafe(32)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(instance_id)
            stream.flush()
            os.fsync(stream.fileno())
        return instance_id

    # Another launcher can win O_EXCL and still be writing the marker. Wait a
    # short, bounded interval for its complete value rather than treating that
    # normal startup race as a corrupt workspace.
    deadline = time.monotonic() + 2
    while True:
        try:
            instance_id = marker.read_text(encoding="ascii").strip()
        except OSError:
            instance_id = ""
        if len(instance_id) >= 32:
            return instance_id
        if time.monotonic() >= deadline:
            raise OSError(f"workspace instance marker is empty or invalid: {marker}")
        time.sleep(0.02)


def _workspace_instance_is_running(origin: str, instance_id: str) -> bool:
    """Return true only when the health endpoint proves the instance identity."""
    try:
        with urllib.request.urlopen(f"{origin}/api/v1/health", timeout=1) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("workspace_instance_id") == instance_id
    )


def _open_when_healthy(origin: str) -> None:
    health_url = f"{origin}/api/v1/health"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                if response.status == 200:
                    if not webbrowser.open_new_tab(origin):
                        print(f"Open the workspace in your browser: {origin}")
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    print(f"Workspace did not become healthy in time. Open {origin} manually.")


def positive_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def add_workspace_ui_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "ui",
        help="Launch the local browser workspace.",
        description=(
            "Start the browser workspace on loopback. Workspace records are kept "
            "in the current user's application data directory by default."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="Register this Git repository when the workspace starts.",
    )
    parser.add_argument("--port", type=positive_port, default=8765)
    parser.add_argument(
        "--no-open", action="store_true", help="Do not open a browser automatically."
    )
    parser.add_argument(
        "--data-dir",
        "--data-directory",
        dest="data_dir",
        type=Path,
        help="Override the OS-specific workspace data directory.",
    )
    parser.set_defaults(func=run_workspace_ui)
