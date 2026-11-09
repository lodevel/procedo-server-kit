"""Who the app is on this machine: the names its data folders and its QSettings are keyed by.

One place, so the Procedo rename switches them once, and a test
redirects them once (tests/conftest.py) — the four folder builders and the QSettings
constructions each spelled the name on their own, and QSettings went to the real registry from
the tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtCore import QSettings

#: The folder name under the per-user roaming and local data roots.
DATA_DIR_NAME = "Procedo"
#: The QSettings organization (on Windows: HKCU\\Software\\<this>).
SETTINGS_ORGANIZATION = "Procedo"
#: The application name (QCoreApplication; the default QSettings application).
APPLICATION_NAME = "Procedo"
#: The Windows taskbar identities (AppUserModelID) of Bench and Studio.
BENCH_APP_ID = "Procedo.Bench"
STUDIO_APP_ID = "Procedo.Studio"
#: Procedo Studio's own settings folder, inside the roaming folder.
_STUDIO_DIR_NAME = "studio"
#: Procedo Studio's default launch command (Settings, the QSettings key WorkflowEditor/Command).
STUDIO_COMMAND = '"{python}" -m procedo.studio'


def _roaming_base() -> Path:
    if os.name == "nt":
        # An EMPTY variable (a stripped environment, a launcher passing ``APPDATA=``) is
        # ``Path(".")``: the folder would land in the working directory, often a project.
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return Path.home() / ".config"


def _local_base() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return Path.home() / ".cache"


def roaming_data_dir() -> Path:
    """The per-user settings folder (``%APPDATA%\\<name>``; ``~/.config/<name>``)."""
    return _roaming_base() / DATA_DIR_NAME


def local_data_dir() -> Path:
    """The per-machine data folder (``%LOCALAPPDATA%\\<name>``; ``~/.cache/<name>``): the
    outbox, the evidence cache, the projects' local state, the environment pool."""
    return _local_base() / DATA_DIR_NAME


def studio_settings_dir() -> Path:
    """Procedo Studio's settings folder (its LLM backends and their keys), in the roaming folder."""
    return roaming_data_dir() / _STUDIO_DIR_NAME


def claim_taskbar_identity(app_id: str) -> None:
    """Give this process its own Windows taskbar identity; call it before the first window shows.

    Without it Windows groups the window under python.exe and shows Python's icon instead of the
    window icon.
    """
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)


def settings(application: str = APPLICATION_NAME) -> QSettings:
    """The app's QSettings for *application* (one group of window state per name)."""
    from PySide6.QtCore import QSettings

    return QSettings(SETTINGS_ORGANIZATION, application)
