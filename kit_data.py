"""Write a known data set to your server, then check later that every item and every byte of it
is still there and the same: around a backup and its restore, or an upgrade.

    python kit_data.py fill   [--manifest kit_dataset.json]
    python kit_data.py verify [--manifest kit_dataset.json]

``fill`` writes a project (files, a bundle, changes, two releases), a library item, runs with
their files on a board, annotation and board entries, a report in two generations, an export,
and a run and an export deleted (in the trash), all under names of its own; it reads it all back
and writes the manifest, a local file: every read with the answer your server gave, and every
blob. ``verify`` makes the same reads and downloads every blob again, and lists each difference
(exit status 1), or says that nothing differs (0).

It talks to the server the kit's variables name (``PROCEDO_KIT_SERVER``,
``PROCEDO_KIT_BENCH_TOKEN``, ``PROCEDO_KIT_MANAGER_TOKEN``, ``PROCEDO_KIT_CA_BUNDLE``); the
README says when to run each.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_KIT = Path(__file__).resolve().parent
sys.path[:0] = [str(_KIT / "vendor"), str(_KIT / "suites")]

import server_dataset  # noqa: E402  (suites/)
import server_target  # noqa: E402  (suites/)

NEEDED = (server_target.SERVER_ENV, "PROCEDO_KIT_BENCH_TOKEN", "PROCEDO_KIT_MANAGER_TOKEN")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("fill", "verify"))
    parser.add_argument(
        "--manifest", type=Path, default=Path("kit_dataset.json"), help="the manifest's file"
    )
    args = parser.parse_args(argv)
    unset = [name for name in NEEDED if not os.environ.get(name, "").strip()]
    if unset:
        print(f"kit_data: set {', '.join(unset)} first (see README.md)", file=sys.stderr)
        return 2
    base = server_target.external()
    try:
        if args.action == "fill":
            if args.manifest.exists():
                print(f"kit_data: {args.manifest} exists: verify it, or name another", file=sys.stderr)
                return 2
            written = server_dataset.fill(base, args.manifest)
            print(f"Wrote the data set (project {written['project_id']}, {len(written['reads'])} "
                  f"reads, {len(written['blobs'])} blobs) and {args.manifest}.")  # fmt: skip
            return 0
        problems = server_dataset.verify(base, args.manifest)
    except server_dataset.DatasetError as exc:
        print(f"kit_data: {exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(f"DIFFERS  {problem}")
    if problems:
        print(f"{len(problems)} differences between {base} and {args.manifest}.")
        return 1
    print(f"Everything in {args.manifest} is on {base}, the same.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
