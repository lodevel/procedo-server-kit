"""Many benches at once against the server under test: what each real bench does, all together,
then everything read back.

Each simulated bench is a thread with its own connection and ``machine_id``, testing a board of
its own in one shared project, round after round: it pushes a run with its record and a picture
(E1), uploads a report (E10), annotates the run (E7), sets the board's lot (E15), and reads the
run and the report back (E3, E9), comparing every field and byte with what it sent; the lot
only when the server offers E15 (§6.1). When every bench is done, each run is read again and
each board listed (E5): nothing may be missing.

:func:`run` **fails** (its :class:`Load`'s ``failures``) on anything lost, refused or read back
different. A ``429`` or ``503`` carrying ``Retry-After`` is not a failure: the bench waits as the
app does and sends the same request again (counted in ``retries``). It **reports** each call's
response times (median, p95, max), never failing on them.
"""

from __future__ import annotations

import hashlib
import secrets
import statistics
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pytest
import server_target
from test_server_contract_evidence import TEST_A, Evidence, Wire, board_entry, comment, parse

#: The longest wait a ``Retry-After`` is followed for, in seconds.
MAX_RETRY_WAIT = 30.0
RETRIES = 5


@dataclass
class Load:
    """What a load run saw: failures, retries, and each call's times in seconds."""

    benches: int
    seconds: float = 0.0
    rounds: int = 0
    failures: list[str] = field(default_factory=list)
    retries: int = 0
    times: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def failed(self, what: str) -> None:
        with self.lock:
            self.failures.append(what)

    def took(self, call: str, seconds: float) -> None:
        with self.lock:
            self.times[call].append(seconds)

    def report(self) -> str:
        """The response times, one line per call: count, median, p95, max (ms)."""
        lines = [
            f"Load: {self.benches} benches, {self.rounds} rounds in {self.seconds:.1f} s, "
            f"{len(self.failures)} failures, {self.retries} retries (Retry-After followed)",
            f"{'call':<28}{'count':>7}{'median ms':>11}{'p95 ms':>9}{'max ms':>9}",
        ]
        for call in sorted(self.times):
            values = sorted(self.times[call])
            p95 = values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]
            lines.append(
                f"{call:<28}{len(values):>7}{statistics.median(values) * 1000:>11.1f}"
                f"{p95 * 1000:>9.1f}{values[-1] * 1000:>9.1f}"
            )
        return "\n".join(lines)


class _Bench:
    """One simulated bench."""

    def __init__(
        self, number: int, base_url: str, project: str, store: int, load: Load, offered: frozenset
    ) -> None:
        #: The optional calls the server offers (§6.1): a bench sets a lot (E15) only when it
        #: offers it.
        self.offered = offered
        self.wire = Wire(base_url)
        self.ev = Evidence(self.wire, project, store)
        self.load = load
        self.machine = secrets.token_hex(4)
        self.serial = f"LOAD-{number:03d}-{secrets.token_hex(3)}"
        self.runs: dict[str, dict] = {}  # run_id -> the run sent
        self.notes: dict[str, str] = {}  # run_id -> the annotation entry sent
        self.reports: dict[str, bytes] = {}  # name -> bytes

    def call(self, name: str, send: Any) -> Any:
        """*send()* timed; a ``429``/``503`` with ``Retry-After`` waited out and sent again."""
        for _attempt in range(RETRIES):
            started = time.perf_counter()
            reply = send()
            self.load.took(name, time.perf_counter() - started)
            wait = reply.headers.get("Retry-After", "")
            if reply.status in (429, 503) and wait.isdigit():
                with self.load.lock:
                    self.load.retries += 1
                time.sleep(min(float(wait), MAX_RETRY_WAIT))
                continue
            return reply
        return reply

    def answered(self, name: str, reply: Any) -> dict | None:
        if reply.status != 200:
            self.load.failed(f"{self.serial} {name}: {reply.status} {reply.body[:300]!r}")
            return None
        try:
            return parse(reply)[0]
        except (AssertionError, ValueError) as exc:
            self.load.failed(f"{self.serial} {name}: an answer the contract does not allow: {exc}")
            return None

    def one_round(self, number: int) -> None:
        ev = self.ev
        run = ev.run(serial=self.serial, start=number * 10, ended=number * 10 + 5,
                     machine_id=self.machine)  # fmt: skip
        picture = secrets.token_bytes(20_000 + secrets.randbelow(180_000))
        run["files"].append(ev.file(f"scope_{number}.png", picture, "attachment", "image/png"))
        pushed = self.answered("E1 push run", self.call("E1 push run", lambda: ev.push(run)))
        if pushed is None:
            return
        if pushed.get("status") != "saved":
            self.load.failed(f"{self.serial} E1 answered {pushed.get('status')!r}, not saved")
            return
        self.runs[run["run_id"]] = run
        name = f"report-{number}.html"
        data = f"<p>{self.serial} round {number} {secrets.token_hex(16)}</p>".encode()
        art = ev.artifact(
            "report", name, self.serial, "AX", "B", data=data, machine_id=self.machine
        )
        reply = self.call("E10 push report", lambda: ev.push_artifact(art))
        if self.answered("E10 push report", reply):
            self.reports[name] = data
        note = comment("test", f"{self.serial} round {number}", None, machine_id=self.machine)
        reply = self.call("E7 annotate", lambda: ev.annotate(run["run_id"], [note]))
        if self.answered("E7 annotate", reply):
            self.notes[run["run_id"]] = note["entry_id"]
        if "E15" in self.offered:
            lot = board_entry("lot", f"L-{self.serial}-{number}", None)
            query = {"part_number": "AX", "board_revision": "B"}
            path = ev.path("boards", self.serial, "entries")
            reply = self.call("E15 board entry", lambda: self.wire.send(
                "POST", path, query=query, body={"entries": [lot]}))  # fmt: skip
            self.answered("E15 board entry", reply)
        self.check_run(run["run_id"], with_parts=True)
        self.check_report(name)

    def check_run(self, run_id: str, *, with_parts: bool) -> None:
        sent = self.runs[run_id]
        held = None if with_parts else ",".join(f"sha256:{f['sha256']}" for f in sent["files"])
        reply = self.call("E3 read run", lambda: self.wire.send(
            "GET", self.ev.path("runs", run_id), query={"have": held} if held else None))  # fmt: skip
        if reply.status != 200:
            self.load.failed(f"{self.serial} run {run_id} lost: E3 answered {reply.status}")
            return
        try:
            read, parts = parse(reply)
        except (AssertionError, ValueError) as exc:
            self.load.failed(f"{self.serial} run {run_id}: {exc}")
            return
        for key, value in sent.items():
            if read.get(key) != value:
                self.load.failed(f"{self.serial} run {run_id}: {key} reads back {read.get(key)!r}")
        if with_parts:
            for item in sent["files"]:
                data = parts.get(f"sha256:{item['sha256']}")
                if data is None or hashlib.sha256(data).hexdigest() != item["sha256"]:
                    self.load.failed(f"{self.serial} run {run_id}: {item['name']} not read back")
        entries = [e["entry_id"] for e in read.get("annotation", {}).get("entries", [])]
        if run_id in self.notes and entries != [self.notes[run_id]]:
            self.load.failed(f"{self.serial} run {run_id}: annotation entries {entries}")

    def check_report(self, name: str) -> None:
        if name not in self.reports:
            return
        reply = self.call("E9 read report", lambda: self.ev.get_artifact(
            "report", name, self.serial, "AX", "B"))  # fmt: skip
        answer = self.answered("E9 read report", reply)
        if answer is None:
            return
        if "store_id" not in answer:  # kept as its record alone (§5.10): the record is there
            if not isinstance(answer.get("info"), dict):
                self.load.failed(f"{self.serial} report {name}: its record is not read back")
            return
        data = parse(reply)[1].get(f"sha256:{answer.get('sha256')}")
        if data != self.reports[name]:
            self.load.failed(f"{self.serial} report {name}: other bytes read back")

    def check_everything(self) -> None:
        """After the load: every run read again, the board lists every run."""
        for run_id in self.runs:
            self.check_run(run_id, with_parts=False)
        reply = self.call("E5 board", lambda: self.wire.send(
            "GET", self.ev.path("boards", self.serial),
            query={"part_number": "AX", "board_revision": "B"}))  # fmt: skip
        board = self.answered("E5 board", reply)
        if board is None:
            return
        listed = {r["run_id"] for t in board["tests"] if t["test_id"] == TEST_A for r in t["runs"]}
        lost = sorted(set(self.runs) - listed)
        if lost:
            self.load.failed(f"{self.serial}: E5 does not list {len(lost)} runs: {lost[:3]}")

    def close(self) -> None:
        self.wire.close()


def run(base_url: str, *, benches: int, seconds: float = 0.0, rounds: int = 3) -> Load:
    """*benches* benches at once against the server at *base_url*: each does *rounds* rounds, or
    as many as fit in *seconds* when given (a soak run). The :class:`Load` seen."""
    load = Load(benches)
    wire = Wire(base_url)
    try:
        storages = parse(wire.send("GET", "storages"))[0]["storages"]
    finally:
        wire.close()
    found = [s["store_id"] for s in storages if s["backend"] == "api"]
    if not found:
        load.failed("the server lists no api storage: the benches send their bytes there")
        return load
    project = server_target.fresh_id()
    offered = server_target.offered(base_url)
    every = [_Bench(n, base_url, project, found[0], load, offered) for n in range(benches)]
    started = time.monotonic()
    counts = [0] * benches

    def work(index: int) -> None:
        bench = every[index]
        try:
            while True:
                if seconds and time.monotonic() - started >= seconds and counts[index]:
                    break
                if not seconds and counts[index] >= rounds:
                    break
                bench.one_round(counts[index])
                counts[index] += 1
        except (Exception, pytest.fail.Exception) as exc:  # a bench that stops: a failure
            load.failed(f"{bench.serial} stopped: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=work, args=(n,), daemon=True) for n in range(benches)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    load.seconds = time.monotonic() - started
    load.rounds = sum(counts)
    for bench in every:
        try:
            bench.check_everything()
        except (Exception, pytest.fail.Exception) as exc:
            load.failed(f"{bench.serial} read back: {type(exc).__name__}: {exc}")
        finally:
            bench.close()
    return load
