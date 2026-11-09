"""What was read from a project's files, kept until something says they changed."""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

#: Every cache, by id (a dict is unhashable), for :func:`written`.
_LIVE: weakref.WeakValueDictionary[int, FileCache] = weakref.WeakValueDictionary()
_live_lock = threading.Lock()  # a cache registers on the GUI thread; a worker may announce a write


def written(path: Path) -> None:
    """The app just wrote *path*: every cache forgets what it read in that file's folder."""
    with _live_lock:
        caches = list(_LIVE.values())
    for cache in caches:
        cache.forget(path.parent)


def read(path: Path, files: FileCache | None = None) -> str | OSError | ValueError:
    """*path*'s UTF-8 text, or why it cannot be read (ValueError: not UTF-8): from *files*
    when the caller has the open project's cache (each file read once until it changes),
    else from disk."""
    if files is not None:
        return files.get_or_compute("text", path, read)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return exc


def read_text(path: Path, files: FileCache | None = None) -> str | None:
    """:func:`read`, None when the file cannot be read."""
    text = read(path, files)
    return text if isinstance(text, str) else None


def exists(path: Path, files: FileCache | None = None) -> bool:
    """Whether *path* exists: from *files* when the caller has the open project's cache."""
    return files.exists(path) if files is not None else path.exists()


#: How many reads run at once (:func:`in_parallel`): a share answers each round trip slowly,
#: but several at a time.
READERS = 8


def in_parallel(work: Callable[[T], object], items: Iterable[T]) -> None:
    """``work(item)`` for every item, :data:`READERS` at a time — for a project's files read in
    bulk (a test's each). Returns once all are done; the first exception raised is raised here.
    The readers are named after the thread that asked (``project-read-reader_0``…): its work."""
    name = f"{threading.current_thread().name}-reader"
    with ThreadPoolExecutor(READERS, thread_name_prefix=name) as pool:
        for future in [pool.submit(work, item) for item in items]:
            future.result()


class FileCache(dict):
    """``(kind, path) -> value``, read once and then served from memory: never a ``stat``
    per read — on a network share each is a round trip, and the GUI asks on every refresh.
    Whatever changes the files says so with :meth:`forget`: a rescan (everything), the
    project's folder watcher (the folder that changed), an in-app writer (:func:`written`).

    Any thread may read through it or forget: a value computed while something was
    forgotten is answered but not kept (it may be what the file held before the change)."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._generation = 0  # moves on every forget
        #: While an F5 reads off the GUI thread (``project_opening``): every folder :meth:`forget`
        #: was told of since it began (None: everything), forgotten again in what it read
        #: (``ProjectModel.adopt``). None the rest of the time: nothing is kept.
        self.forgotten: list[Path | None] | None = None
        if threading.current_thread() is threading.main_thread():
            self.register()

    def register(self) -> None:
        """Hear :func:`written` from now on. A cache filled on a worker (the project open)
        registers once it is handed to the GUI thread, where :func:`written` runs."""
        with _live_lock:
            _LIVE[id(self)] = self

    def get_or_compute(
        self,
        kind: str,
        path: Path,
        compute: Callable[[Path], T],
        keep: Callable[[], bool] = lambda: True,
    ) -> T:
        """*compute(path)* the first time *kind* is asked of *path*, the kept value after —
        kept only when *keep()* says so once it is computed."""
        key = (kind, path)
        with self._lock:
            if key in self:
                return self[key]
            generation = self._generation
        value = compute(path)
        with self._lock:
            if generation == self._generation and keep():
                self[key] = value
        return value

    def exists(self, path: Path) -> bool:
        return self.get_or_compute("exists", path, Path.exists)

    def kept_text(self, path: Path) -> str | None:
        """*path*'s text as already read; None when it was not, or could not be. Never reads it."""
        text = self.get(("text", path))
        return text if isinstance(text, str) else None

    def forget(self, folder: Path | None = None) -> None:
        """Drop what was read in *folder* or below it; everything when None."""
        with self._lock:
            self._generation += 1
            if self.forgotten is not None:
                self.forgotten.append(folder)
            for key in [
                k for k in self if folder is None or k[1] == folder or folder in k[1].parents
            ]:
                del self[key]

    def discard(self, kind: str, path: Path) -> None:
        """Drop what was kept of *kind* for *path* (a value computed meanwhile is not kept)."""
        with self._lock:
            self._generation += 1
            self.pop((kind, path), None)

    def take(self, other: FileCache, changed: list[Path | None]) -> None:
        """Keep what *other* read instead of all that was kept here — a fresh read of the
        project (F5) — except in the folders *changed* while it was read (None: all of them)."""
        for folder in changed:
            other.forget(folder)
        with self._lock:
            self._generation += 1
            if self.forgotten is not None:
                self.forgotten.append(None)
            self.clear()
            self.update(other)

    def start_noting(self) -> None:
        """An F5 begins its read: note every folder :meth:`forget` is told of from now on."""
        self.forgotten = []

    def stop_noting(self) -> list[Path | None]:
        """The F5 is over: what was forgotten since :meth:`start_noting`; nothing is kept."""
        noted, self.forgotten = self.forgotten or [], None
        return noted
