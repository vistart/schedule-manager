"""Load benchmark for the MCP server under concurrent, multi-user traffic.

Opt-in — it saturates the database and takes minutes:

    SCHEDULE_BENCH=1 pytest tests/test_benchmark.py -v -s

    # knobs
    SCHEDULE_BENCH_SEED=20260928   reproducibility; same seed, same dataset
    SCHEDULE_BENCH_USERS=8         distinct identities
    SCHEDULE_BENCH_PER_USER=200    schedules seeded per identity
    SCHEDULE_BENCH_STEPS=1,4,8,16,32   concurrency levels
    SCHEDULE_BENCH_REQUESTS=300    requests per level
    SCHEDULE_BENCH_POOL_MAX=64     pool ceiling for the server under test

What this is and is not.  Absolute throughput depends on the link to Postgres,
so the numbers are only meaningful next to the pool ceiling printed in the
report.  What the run *does* establish is independent of the hardware:

* no connection or transaction leak — pool stats are printed before and after
* no cross-user leakage under load, asserted on every response
* no 5xx, and latency distribution rather than a single mean

Correctness is checked *during* the load.  A throughput number is worthless if
the run quietly served another user's rows.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import random
import socket
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SCHEDULE_BENCH") != "1",
    reason="set SCHEDULE_BENCH=1 to run the load benchmark",
)

# Before anything touches the ORM: it logs every statement at DEBUG, and
# seeding a few thousand rows would bury the report it is meant to produce.
logging.getLogger("rhosocial").setLevel(logging.WARNING)

SEED = int(os.environ.get("SCHEDULE_BENCH_SEED", "20260928"))
USERS = int(os.environ.get("SCHEDULE_BENCH_USERS", "8"))
PER_USER = int(os.environ.get("SCHEDULE_BENCH_PER_USER", "200"))
REQUESTS = int(os.environ.get("SCHEDULE_BENCH_REQUESTS", "300"))
STEPS = [int(s) for s in os.environ.get("SCHEDULE_BENCH_STEPS", "1,4,8,16,32").split(",")]
POOL_MAX = int(os.environ.get("SCHEDULE_BENCH_POOL_MAX", "64"))
# Each level of the connection sweep starts ``level`` server processes, so the
# ceiling is practical rather than arbitrary: 32 processes means 32 cold starts
# (connect + dialect introspection + DDL) against a remote database.
CONN_STEPS = [
    int(s)
    for s in os.environ.get("SCHEDULE_BENCH_CONN_STEPS", "1,2,4,8,16").split(",")
]

VENV_PYTHON = sys.executable
MCP_PATH = "/mcp"

# A mix that is read-heavy, like real use, but keeps writes in the run: a write
# path that never executes under load is not a write path that has been tested.
READ_SHARE = 0.70


@dataclass
class Counter:
    status: dict[int, int] = field(default_factory=dict)
    leaks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Cross-user probes that were correctly refused.  Non-zero proves the
    #: isolation path was actually exercised rather than skipped.
    refused: int = 0
    #: Successful writes, so list totals can account for them.
    writes: int = 0

    def record(self, status: int) -> None:
        self.status[status] = self.status.get(status, 0) + 1


@dataclass
class LevelResult:
    concurrency: int
    seconds: float
    latencies: list[float]
    counter: Counter

    @property
    def throughput(self) -> float:
        return len(self.latencies) / self.seconds if self.seconds else 0.0

    def percentile(self, p: float) -> float:
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        index = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
        return ordered[index]


REPORT_PATH = os.environ.get("SCHEDULE_BENCH_REPORT", "/tmp/schedule-bench-report.txt")


def report(line: str) -> None:
    """Print and append immediately.

    A long benchmark can be killed part way through; without this the buffered
    output is lost and the run produces nothing.
    """
    print(line, flush=True)
    try:
        with open(REPORT_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass



def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Server:
    """The MCP server under test, in its own process with its own pool.

    stderr goes to a file, never a pipe: an unread pipe fills its 64 KiB buffer
    and the server then blocks on its own log write, which looks exactly like
    the service falling over under load.
    """

    def __init__(
        self,
        port: int,
        pool_max: int | None = None,
        *,
        migrate: bool = True,
        workers: int = 1,
    ) -> None:
        self.port = port
        self.pool_max = pool_max
        self.migrate = migrate
        self.workers = workers
        self.proc: subprocess.Popen | None = None
        self.log_path = f"/tmp/schedule-bench-{port}.log"
        self._log = None

    def __enter__(self) -> "Server":
        env = dict(os.environ)
        env["SCHEDULE_BIND_HOST"] = "127.0.0.1"
        env["SCHEDULE_BIND_PORT"] = str(self.port)
        env["SCHEDULE_PUBLIC_URL"] = f"http://127.0.0.1:{self.port}"
        env["SCHEDULE_ALLOWED_HOSTS"] = f"127.0.0.1:*"
        env["SCHEDULE_ALLOWED_ORIGINS"] = f"http://127.0.0.1:*"
        env["SCHEDULE_POOL_MIN"] = "1"
        env["SCHEDULE_POOL_MAX"] = str(self.pool_max or POOL_MAX)
        env["SCHEDULE_LOG_LEVEL"] = "ERROR"
        env["SCHEDULE_AUTO_MIGRATE"] = "1" if self.migrate else "0"
        env["SCHEDULE_WORKERS"] = str(self.workers)
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [VENV_PYTHON, "-m", "schedule_manager.mcp_server"],
            env=env,
            stdout=self._log,
            stderr=self._log,
            start_new_session=True,   # so the worker's children die with it
        )
        self._await_ready()
        return self

    def _await_ready(self, timeout: float = 90.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(
                    f"server exited early; see {self.log_path}\n{self._tail()}"
                )
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                time.sleep(0.4)
        raise RuntimeError(
            f"server did not start listening in time; see {self.log_path}\n{self._tail()}"
        )

    def _tail(self, limit: int = 800) -> str:
        try:
            with open(self.log_path, "rb") as handle:
                return handle.read()[-limit:].decode(errors="replace")
        except OSError:
            return "(no log)"

    def __exit__(self, *exc) -> None:
        if self.proc:
            # uvicorn's supervisor spawns worker children; signalling the process
            # group is what actually takes them down.
            try:
                os.killpg(os.getpgid(self.proc.pid), 15)
            except (ProcessLookupError, PermissionError):
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log:
            self._log.close()


class Client:
    """Keep-alive JSON-RPC caller. One connection per worker thread."""

    def __init__(self, port: int, token: str) -> None:
        self._port = port
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        self.token = token

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[int, Any]:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        try:
            self.conn.request("POST", MCP_PATH, body=body, headers=headers)
            response = self.conn.getresponse()
            raw = response.read().decode()
        except (http.client.HTTPException, OSError):
            self.conn.close()
            self.conn = http.client.HTTPConnection("127.0.0.1", self._port, timeout=60)
            self.conn.request("POST", MCP_PATH, body=body, headers=headers)
            response = self.conn.getresponse()
            raw = response.read().decode()
        return response.status, _extract(raw)

    def close(self) -> None:
        self.conn.close()


def _extract(raw: str) -> Any:
    """Pull the tool result out of an SSE frame."""
    for line in raw.splitlines():
        if line.startswith("data: "):
            try:
                envelope = json.loads(line[6:])
            except ValueError:
                continue
            content = (envelope.get("result") or {}).get("content") or []
            for item in content:
                text = item.get("text")
                if text and item.get("type") == "text":
                    try:
                        return json.loads(text)
                    except ValueError:
                        return text
            return envelope.get("result")
    return None


# ── Connection sweep ────────────────────────────────────────────────────────
#
# The concurrency sweep above varies *client* concurrency against a single
# server process.  That cannot answer "how many database connections before
# latency degrades", for two reasons:
#
#   * concurrency is not connections — the pool reuses them, and a request makes
#     two or three round trips while holding one;
#   * one server process is one event loop, so a plateau at concurrency 8 may
#     be that loop serialising Python rather than the database giving out.
#
# So this mode makes connections the independent variable: ``N`` server
# processes, each with ``pool_max=1`` (hence one connection each), driven by
# ``N`` separate client *processes*.  Nothing is shared, so the only thing
# scaling is the connection count against the database.


async def _db_connections() -> int:
    """Connections the server currently holds to this database."""
    from schedule_manager.config import get_db_config
    from schedule_manager.db import close_pool, connection, create_pool

    pool = await create_pool(get_db_config())
    try:
        async with connection(pool) as backend:
            rows = await backend.fetch_all(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE datname = current_database()",
                None,
            )
            return int(rows[0]["n"])
    finally:
        await close_pool(pool)


def _client_worker(port: int, token: str, count: int) -> dict:
    """Run as its own process, so the GIL cannot be the limit."""
    latencies = []
    client = Client(port, token)
    try:
        for _ in range(count):
            started = time.perf_counter()
            client.call("list_schedules", {"page_size": 20})
            latencies.append(time.perf_counter() - started)
    finally:
        client.close()
    return {"latencies": latencies}


@pytest.mark.skipif(
    os.environ.get("SCHEDULE_BENCH_CONN") != "1",
    reason="set SCHEDULE_BENCH_CONN=1 to sweep the connection count",
)
def test_connection_sweep(seeded):
    """Latency as a function of database connections, not of client threads."""
    ports: list[int] = []
    servers: list[Server] = []
    try:
        for level in CONN_STEPS:
            # Tear down the previous level so the count is not cumulative.
            for running in servers:
                running.__exit__(None, None, None)
            servers = []
            ports = []

            for _ in range(level):
                port = _free_port()
                # Schema is ensured once by the `seeded` fixture; letting every
                # instance do it would serialise the level on DDL locks.
                server = Server(port, pool_max=1, migrate=False)
                server.__enter__()
                servers.append(server)
                ports.append(port)

            tokens = [row["token"] for row in seeded[:level]]
            per_client = max(1, REQUESTS // level)
            workers = [
                subprocess.Popen(
                    [VENV_PYTHON, __file__, "client",
                     str(port), token, str(per_client)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=os.environ,
                )
                for port, token in zip(ports, tokens)
            ]
            latencies: list[float] = []
            failed = 0
            for worker in workers:
                out, err = worker.communicate(timeout=600)
                if worker.returncode != 0:
                    failed += 1
                    print(f"    client failed: {err.decode()[-200:]}")
                    continue
                latencies.extend(json.loads(out)["latencies"])
            for running in servers:
                running.__exit__(None, None, None)
            servers = []

            if not latencies:
                report(f"[conn] {level:>3} connections: all clients failed")
                continue
            mean = statistics.fmean(latencies)
            print(
                f"[conn] {level:>3} conn  n={len(latencies):5d}  "
                f"p50={_pct(latencies, 50) * 1000:7.1f} ms  "
                f"p95={_pct(latencies, 95) * 1000:7.1f} ms  "
                f"p99={_pct(latencies, 99) * 1000:7.1f} ms  "
                f"mean={mean * 1000:7.1f} ms  failed={failed}"
            )
    finally:
        for running in servers:
            running.__exit__(None, None, None)


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return ordered[index]



# ── Worker scaling ──────────────────────────────────────────────────────────
#
# One uvicorn worker is one event loop on one core.  On a 16-core host that is
# the ceiling long before the database is, so this mode varies the worker count
# and reports what the service can actually serve — with the connection count
# the server will open alongside it (``workers * pool_max``).

#: ``SCHEDULE_BENCH_WORKERS`` enables this test; the levels come from their own
#: variable so that turning the test on does not also pin the level list.
WORKER_LEVELS = [
    int(w)
    for w in os.environ.get("SCHEDULE_BENCH_WORKER_LEVELS", "1,2,4,8,16").split(",")
]


@pytest.mark.skipif(
    os.environ.get("SCHEDULE_BENCH_WORKERS") != "1",
    reason="set SCHEDULE_BENCH_WORKERS=1 to measure worker scaling",
)
def test_worker_scaling(seeded):
    # The total connection budget is held constant across levels, so the only
    # thing that changes is how those connections are spread over workers.  That
    # also keeps the server inside the database's own max_connections=100.
    budget = int(os.environ.get("SCHEDULE_BENCH_WORK_BUDGET", "32"))
    concurrency = int(os.environ.get("SCHEDULE_BENCH_WORK_CONC", "32"))
    cast = [row["token"] for row in seeded]
    while len(cast) < concurrency:
        cast.extend(cast)
    cast = cast[:concurrency]

    report(f"{'=' * 76}")
    report(f"[work] workers x concurrency={concurrency} "
           f"connection budget={budget} requests={REQUESTS}")
    report(f"[work] {'workers':>8} {'conns':>7} {'req/s':>9} {'p50 ms':>8} "
           f"{'p95 ms':>8} {'p99 ms':>8} {'max ms':>8} {'bad':>5}")
    report("-" * 76)

    for workers in WORKER_LEVELS:
        pool_max = max(1, budget // workers)
        port = _free_port()
        server = Server(port, pool_max=pool_max, migrate=False, workers=workers)
        try:
            server.__enter__()
            # One client process per worker keeps the client from being the cap.
            per_client = max(5, REQUESTS // workers)
            procs = [
                subprocess.Popen(
                    [VENV_PYTHON, __file__, "client", str(port), token, str(per_client)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ,
                )
                for token in cast
            ]
            latencies: list[float] = []
            bad = 0
            started = time.perf_counter()
            for proc in procs:
                out, err = proc.communicate(timeout=600)
                if proc.returncode != 0:
                    bad += 1
                    print(f"    client failed: {err.decode()[-200:]}")
                    continue
                latencies.extend(json.loads(out)["latencies"])
            wall = time.perf_counter() - started
            if not latencies:
                report(f"[work] {workers:>8} all clients failed")
                continue
            ordered = sorted(latencies)
            report(
                f"[work] {workers:>8} {workers}x{pool_max:<2}={workers * pool_max:<3} "
                f"{len(latencies) / wall:>9.1f} "
                f"{ordered[int(0.50 * (len(ordered) - 1))] * 1000:>8.1f} "
                f"{ordered[int(0.95 * (len(ordered) - 1))] * 1000:>8.1f} "
                f"{ordered[int(0.99 * (len(ordered) - 1))] * 1000:>8.1f} "
                f"{ordered[-1] * 1000:>8.1f} {bad:>5}"
            )
        finally:
            server.__exit__(None, None, None)
    report("-" * 76)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
async def seeded():
    """Create the dataset: N identities with M schedules each, plus tokens."""
    from schedule_manager.config import configure_logging, get_db_config
    from schedule_manager.db import close_pool, connection, create_pool
    from schedule_manager.identity import reset_cli_user, set_cli_user
    from schedule_manager.models import SCOPES, Schedule, User
    from schedule_manager.schema import create_all, drop_all

    configure_logging()
    rng = random.Random(SEED)
    pool = await create_pool(get_db_config())
    try:
        async with connection(pool) as backend:
            await drop_all(backend)
            await create_all(backend)

        identities = []
        async with connection(pool):
            for index in range(USERS):
                user, token = await User.open_account(
                    f"bench{index}_{SEED}", scopes=SCOPES, label=f"bench-{index}"
                )
                set_cli_user(user.id, token)
                records = [
                    Schedule(
                        title=f"task-{index}-{n} {rng.choice(('alpha','beta','gamma'))}",
                        description=f"seeded row {n} for identity {index}",
                        priority=rng.randint(1, 5),
                    )
                    for n in range(PER_USER)
                ]
                await Schedule.bulk_create(records, batch_size=200)
                identities.append(
                    {
                        "id": user.id,
                        "ordinal": index,
                        "token": token,
                        "expected": PER_USER,
                    }
                )

        print(f"\n[bench] seeded {USERS} identities x {PER_USER} schedules (seed={SEED})")
        yield identities
    finally:
        await close_pool(pool)


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    with Server(port) as running:
        yield running


# ── Correctness under load ───────────────────────────────────────────────────


def _check_response(
    counter: Counter, status: int, payload: Any, *, allow_not_found: bool = False
) -> None:
    counter.record(status)
    if status >= 500:
        counter.errors.append(f"server error {status}")
        return
    if status != 200:
        counter.errors.append(f"unexpected status {status}")
        return
    if isinstance(payload, dict) and "error" in payload:
        message = str(payload["error"])
        if allow_not_found and "not found" in message:
            counter.refused += 1
            return
        counter.errors.append(f"tool error: {message}")


def _assert_no_leak(counter: Counter, payload: Any, owner: int) -> None:
    """Any item not belonging to ``owner`` is a cross-user leak."""
    if not isinstance(payload, dict):
        return
    for item in payload.get("items", []) or []:
        title = str(item.get("title", ""))
        parts = title.split("-")
        if len(parts) >= 2 and parts[0] == "task" and parts[1].isdigit():
            if int(parts[1]) != owner:
                counter.leaks.append(f"identity {owner} saw {title}")


# ── The load itself ─────────────────────────────────────────────────────────


def _drive(
    port: int,
    identities: list[dict],
    concurrency: int,
    requests: int,
    rng: random.Random,
    writes_so_far: dict[int, int] | None = None,
) -> LevelResult:
    """Run one concurrency level.

    ``writes_so_far`` carries the write count per identity across levels: rows
    created at a lower concurrency are still there at a higher one, so the
    expected list total has to include them.
    """
    writes_so_far = writes_so_far if writes_so_far is not None else {}
    counter = Counter()
    latencies: list[float] = []

    def worker(slot: int, count: int) -> None:
        identity = identities[slot]
        client = Client(port, identity["token"])
        local_rng = random.Random(rng.random())
        try:
            for _ in range(count):
                owner = identity["id"]
                who = identity["ordinal"]
                roll = local_rng.random()
                started = time.perf_counter()
                if roll < READ_SHARE * 0.5:
                    status, payload = client.call(
                        "list_schedules", {"page_size": 100}
                    )
                    _check_response(counter, status, payload)
                    _assert_no_leak(counter, payload, who)
                    if isinstance(payload, dict):
                        # Writes from other threads may not have committed yet,
                        # so an exact total is not assertable during a mixed
                        # read/write load — only a range. The exact figure is
                        # checked once, quiescent, after the run.
                        low = PER_USER
                        high = PER_USER + writes_so_far.get(who, 0) + counter.writes
                        total = payload.get("total")
                        if not isinstance(total, int) or not low <= total <= high:
                            counter.errors.append(
                                f"identity {who} saw total={total}, outside [{low}, {high}]"
                            )
                elif roll < READ_SHARE * 0.7:
                    status, payload = client.call(
                        "search_schedules", {"keyword": local_rng.choice(("alpha", "beta", "gamma"))}
                    )
                    _check_response(counter, status, payload)
                    _assert_no_leak(counter, payload, who)
                elif roll < READ_SHARE * 0.85:
                    # schedule 1 belongs to the first seeded identity, so every
                    # other caller must be refused.
                    status, payload = client.call(
                        "get_schedule", {"schedule_id": 1}
                    )
                    _check_response(counter, status, payload, allow_not_found=True)
                    if isinstance(payload, dict) and "id" in payload:
                        # Schedule 1 belongs to the first seeded identity, so
                        # anybody else getting it back is a leak; its owner
                        # reading it is the expected outcome.
                        title = str(payload.get("title", ""))
                        parts = title.split("-")
                        owner_of_row = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                        if owner_of_row is not None and owner_of_row != who:
                            counter.leaks.append(
                                f"identity {who} read schedule owned by {owner_of_row}"
                            )
                elif roll < READ_SHARE * 0.9:
                    status, payload = client.call(
                        "whoami", {}
                    )
                    _check_response(counter, status, payload)
                    if isinstance(payload, dict) and payload.get("user_id") != owner:
                        counter.errors.append(
                            f"whoami returned {payload.get('user_id')} for {owner}"
                        )
                else:
                    status, payload = client.call(
                        "create_schedule",
                        {"title": f"bench-write-{who}-{local_rng.randrange(10**6)}"},
                    )
                    _check_response(counter, status, payload)
                    if isinstance(payload, dict) and "id" in payload:
                        counter.writes += 1
                        writes_so_far[who] = writes_so_far.get(who, 0) + 1
                latencies.append(time.perf_counter() - started)
        finally:
            client.close()

    per_worker = requests // concurrency
    extra = requests % concurrency
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(worker, slot, per_worker + (1 if slot < extra else 0))
            for slot in range(concurrency)
        ]
        for future in futures:
            future.result()
    seconds = time.perf_counter() - started
    return LevelResult(concurrency, seconds, latencies, counter)


def test_concurrent_load(server, seeded):
    port = server.port
    rng = random.Random(SEED)

    print(f"\n{'=' * 72}")
    print(f"[bench] seed={SEED} identities={USERS} per_identity={PER_USER} "
          f"pool_max={POOL_MAX} requests/level={REQUESTS}")
    print(f"[bench] {'conc':>5} {'req/s':>9} {'p50 ms':>8} {'p95 ms':>8} "
          f"{'p99 ms':>8} {'max ms':>8} {'2xx':>6} {'other':>6}")
    print("-" * 72)

    results: list[LevelResult] = []
    writes_so_far: dict[int, int] = {}
    for concurrency in STEPS:
        # Beyond USERS the slots wrap around, so extra workers share an identity
        # and still have to see only that identity's rows.
        cast = [seeded[slot % len(seeded)] for slot in range(concurrency)]
        result = _drive(port, cast, concurrency, REQUESTS, rng, writes_so_far)
        results.append(result)
        ok = sum(v for k, v in result.counter.status.items() if 200 <= k < 300)
        other = sum(v for k, v in result.counter.status.items() if not 200 <= k < 300)
        print(
            f"[bench] {concurrency:>5} {result.throughput:>9.1f} "
            f"{result.percentile(50) * 1000:>8.1f} {result.percentile(95) * 1000:>8.1f} "
            f"{result.percentile(99) * 1000:>8.1f} "
            f"{max(result.latencies, default=0) * 1000:>8.1f} {ok:>6} {other:>6}"
        )

    print("-" * 72)
    print(f"[bench] peak {max(r.throughput for r in results):.1f} req/s at "
          f"concurrency {max(results, key=lambda r: r.throughput).concurrency}")

    everything = Counter()
    for result in results:
        for code, count in result.counter.status.items():
            everything.status[code] = everything.status.get(code, 0) + count
        everything.leaks.extend(result.counter.leaks)
        everything.errors.extend(result.counter.errors)
        everything.refused += result.counter.refused

    print(f"[bench] status distribution: {dict(sorted(everything.status.items()))}")
    print(f"[bench] cross-user probes correctly refused: {everything.refused}")

    # Quiescent check: with no writes in flight the totals are exact.
    port_final: dict[int, int] = {}
    for identity in seeded:
        client = Client(server.port, identity["token"])
        try:
            status, payload = client.call("list_schedules", {"page_size": 1})
        finally:
            client.close()
        if status != 200:
            counter_errors = [f"final list for {identity['ordinal']} returned {status}"]
            everything.errors.extend(counter_errors)
            continue
        total = payload.get("total")
        port_final[identity["ordinal"]] = total
        expected = PER_USER + writes_so_far.get(identity["ordinal"], 0)
        print(f"[bench]   identity {identity['ordinal']:>2}: "
              f"{PER_USER} seeded + {writes_so_far.get(identity['ordinal'], 0)} written "
              f"= {expected}, server reports {total}")
        if total != expected:
            everything.errors.append(
                f"identity {identity['ordinal']} ended at {total}, expected {expected}"
            )

    assert not everything.leaks, (
        f"cross-user leakage under load ({len(everything.leaks)}): "
        f"{everything.leaks[:5]}"
    )
    assert not everything.errors, (
        f"inconsistencies under load ({len(everything.errors)}): "
        f"{everything.errors[:5]}"
    )
    assert not any(code >= 500 for code in everything.status), (
        f"server errors: {everything.status}"
    )
    assert set(everything.status) == {200}, (
        f"expected only 200 under a valid token, got {everything.status}"
    )
    assert everything.refused > 0, (
        "the cross-user path was never exercised — a green run would mean nothing"
    )
    print("[bench] correctness held: no leakage, no 5xx, only 200")


def main(argv: list[str]) -> int:
    """Entry point so this file can also act as a load-generator process."""
    if len(argv) >= 2 and argv[1] == "client":
        port, token, count = int(argv[2]), argv[3], int(argv[4])
        result = _client_worker(port, token, count)
        print(json.dumps(result))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
