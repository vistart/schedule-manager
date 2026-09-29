#!/usr/bin/env python3
"""Inspect PostgreSQL query statistics via ``pg_stat_statements``.

Answers the two questions that block the caching work: is the extension
usable, and how many statements does one MCP request actually cost.

    python scripts/db_query_stats.py status
    python scripts/db_query_stats.py top --limit 15
    python scripts/db_query_stats.py reset
    python scripts/db_query_stats.py probe --token sm_... --requests 20

``status`` only reads server settings, so it works whether or not the extension
is installed -- that is the point of it, since the fix for a missing extension
lives in ``postgresql.conf`` and cannot be applied from here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── connection ──────────────────────────────────────────────────────────────

def connect():
    import psycopg

    from schedule_manager.config import get_db_config

    cfg = get_db_config()
    conn = psycopg.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        dbname=cfg.database,
        connect_timeout=10,
        # Read-only diagnostics, and several probes are *expected* to fail when
        # the server is not configured.  In a transaction the first failure
        # would poison every later probe into "current transaction is aborted",
        # which reads as a real problem and is not one.
        autocommit=True,
    )
    conn.execute("SET application_name = 'schedule-manager-db-stats'")
    return conn


def scalar(conn, sql: str):
    row = conn.execute(sql).fetchone()
    return row[0] if row else None


def try_scalar(conn, sql: str):
    """Like :func:`scalar` but returns the error instead of raising.

    Reading a GUC that does not exist is how we detect a half-configured
    server, so the failure is the signal rather than something to hide.
    """
    import psycopg

    try:
        return scalar(conn, sql)
    except psycopg.Error as exc:
        return f"<不可用: {str(exc).splitlines()[0]}>"


# ── status ──────────────────────────────────────────────────────────────────

def cmd_status(args) -> int:
    import psycopg

    with connect() as conn:
        version = scalar(conn, "SHOW server_version")
        installed = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'pg_stat_statements'"
        ).fetchone()
        preload = scalar(conn, "SHOW shared_preload_libraries") or ""
        preloaded = "pg_stat_statements" in preload
        track = try_scalar(conn, "SHOW pg_stat_statements.track")
        maxlen = try_scalar(conn, "SHOW pg_stat_statements.max")
        compute_qid = try_scalar(conn, "SHOW compute_query_id")

        usable = None
        entries = None
        try:
            entries = scalar(conn, "SELECT count(*) FROM pg_stat_statements")
            usable = True
        except psycopg.Error:
            usable = False

        print(f"服务器          {version}")
        print(f"扩展已安装      {installed[0] if installed else '否'}")
        print(f"已预加载        {'是' if preloaded else '否'}")
        print(f"shared_preload  {preload or '(空)'}")
        print(f"pg_stat_statements.track   {track}")
        print(f"pg_stat_statements.max     {maxlen}")
        print(f"compute_query_id          {compute_qid}")
        print(f"视图可查      {'是' if usable else '否'}" + (f"（{entries} 条语句）" if usable else ""))

        if usable and entries is not None:
            print()
            print("可用。查看统计： python scripts/db_query_stats.py top")
            return 0

        print()
        print("=" * 68)
        print("未启用。启用需要两步，其中第一步必须在数据库服务器上做：")
        print("=" * 68)
        if not preloaded:
            print()
            print("第 1 步 —— 编辑 192.168.1.3 上的 postgresql.conf：")
            print()
            print("    shared_preload_libraries = 'pg_stat_statements'")
            print("    pg_stat_statements.track = 'all'")
            print()
            print("  然后**重启** postgres（reload 不生效，这是最常见的失败原因）：")
            print()
            print("    sudo systemctl restart postgresql")
        if not installed:
            print()
            print("第 2 步 —— 在 schedule_manager_db 库里建扩展（无需重启）：")
            print()
            print("    CREATE EXTENSION pg_stat_statements;")
        elif not preloaded:
            print()
            print("扩展已建但未预加载 —— 视图存在却无法查询。")
            print("这是最常见的半配置状态：只做了第 2 步。补上第 1 步并重启即可。")
        if not preloaded and not installed:
            print()
            print("两步都做完后重跑本脚本确认。")
        return 1


# ── top ─────────────────────────────────────────────────────────────────────

def cmd_top(args) -> int:
    with connect() as conn:
        try:
            # Column names differ across versions: total_time became
            # total_exec_time in PG 13.  Ask the server rather than guess.
            cols = {r[0] for r in conn.execute(
                "SELECT attname FROM pg_attribute "
                "WHERE attrelid = 'pg_stat_statements'::regclass AND attnum > 0"
            ).fetchall()}
        except Exception as exc:
            print(f"pg_stat_statements 不可用：{str(exc).splitlines()[0]}")
            print("先跑： python scripts/db_query_stats.py status")
            return 1

        total_col = "total_exec_time" if "total_exec_time" in cols else "total_time"
        sql = f"""
            SELECT calls,
                   round({total_col}::numeric, 1)              AS total_ms,
                   round({total_col}::numeric / calls, 3)     AS mean_ms,
                   rows,
                   shared_blks_hit,
                   shared_blks_read,
                   left(query, 70) AS query
            FROM pg_stat_statements
            ORDER BY {total_col} DESC
            LIMIT %s
        """
        rows = conn.execute(sql, (args.limit,)).fetchall()
        if not rows:
            print("还没有记录到任何语句。先跑一段真实流量再回来。")
            return 0

        print(f"{'calls':>9} {'总ms':>11} {'均ms':>9} {'rows':>9} {'命中':>11} {'读盘':>9}  语句")
        print("-" * 100)
        for calls, total_ms, mean_ms, r, hit, read, query in rows:
            text = " ".join(query.split())
            print(f"{calls:>9} {total_ms:>11} {mean_ms:>9} {r:>9} {hit:>11} {read:>9}  {text}")
        print("-" * 100)
        print("命中/读盘来自 shared_blks：命中远大于读盘说明数据在内存里，")
        print("此时瓶颈在 CPU 或应用层，不在磁盘。")
    return 0


def cmd_reset(args) -> int:
    with connect() as conn:
        conn.execute("SELECT pg_stat_statements_reset()")
    print("统计已清零。")
    return 0


# ── probe ───────────────────────────────────────────────────────────────────

#: Counted per statement, not filtered by table: authentication traffic is the
#: thing being measured here, and it lives in sm_api_tokens / sm_users, so a
#: "WHERE query ILIKE '%schedule%'" filter would drop most of the bill.
_BEFORE = "SELECT calls, query FROM pg_stat_statements"

#: The real tool names are create_schedule / get_schedule / list_schedules /
#: ...  Guessing "list" silently produces "Unknown tool" errors that still
#: return HTTP 200 and still authenticate, so the query is under-counted while
#: looking like it worked.
DEFAULT_TOOL = "list_schedules"


def call_once(url: str, token: str, tool: str, args: dict, timeout: float = 30.0):
    import json
    import urllib.request

    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode()

    frames = [line[6:] for line in text.splitlines() if line.startswith("data: ")]
    if not frames:
        raise RuntimeError(f"没有 SSE 数据帧，响应前 200 字符：{text[:200]}")
    result = json.loads(frames[0]).get("result", {})
    # A failing tool is a *successful* JSON-RPC response with isError set, so
    # HTTP 200 proves nothing.  Unknown-tool errors are exactly how a wrong tool
    # name sneaks through and produces a believable-looking measurement.
    if result.get("isError"):
        body = result.get("content") or [{}]
        raise RuntimeError(f"工具返回错误：{str(body[0].get('text'))[:200]}")
    return result


def cmd_probe(args) -> int:
    """Count SQL statements per MCP request by diffing counters around N calls.

    This is the measurement the caching design is blocked on: if authentication
    is not the bulk of the statements, the whole priority order is wrong.
    """
    if not args.token:
        print("--token 必填（SCHEDULE_TOKEN 环境变量亦可）。", file=sys.stderr)
        return 2
    token = args.token or os.environ.get("SCHEDULE_TOKEN")
    url = args.url.rstrip("/") + "/mcp"
    arguments = json.loads(args.arguments) if args.arguments else {}

    with connect() as conn:
        try:
            before = {q: c for c, q in conn.execute(_BEFORE).fetchall()}
        except Exception as exc:
            print(f"pg_stat_statements 不可用：{str(exc).splitlines()[0]}")
            print("先跑： python scripts/db_query_stats.py status")
            return 1

    ok = 0
    started = time.perf_counter()
    for i in range(args.requests):
        try:
            call_once(url, token, args.tool, arguments)
            ok += 1
        except RuntimeError as exc:
            print(f"  第 {i + 1} 次失败：{exc}", file=sys.stderr)
            if ok == 0 and i == 0:
                print("  工具名或参数可能不对。先用 tools/list 确认。", file=sys.stderr)
                return 1
        except Exception as exc:
            print(f"  第 {i + 1} 次连接失败：{exc}", file=sys.stderr)
            return 1
    elapsed = time.perf_counter() - started

    with connect() as conn:
        after = {q: c for c, q in conn.execute(_BEFORE).fetchall()}

    print()
    print(f"工具 {args.tool}  成功 {ok}/{args.requests} 次，耗时 {elapsed:.1f}s"
          f"（{ok / elapsed:.1f} req/s，{elapsed / max(ok, 1) * 1000:.1f} ms/次）")
    if ok == 0:
        return 1

    print()
    print(f"{'每次请求':>9} {'总次数':>8} {'均ms':>8}  语句")
    print("-" * 96)
    total = 0.0
    for query, count in after.items():
        delta = count - before.get(query, 0)
        if delta <= 0:
            continue
        per = delta / ok
        total += per
        text = " ".join(query.split())[:72]
        print(f"{per:>9.2f} {delta:>8} {'':>8}  {text}")
    print("-" * 96)
    print(f"{total:>9.2f} {'':>8} {'':>8}  合计 SQL 次数 / 每次请求")
    print()
    print(f"即每次 {args.tool} 调用打 {total:.0f} 条 SQL。")
    return 0


# ── cli ─────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="检查与查询 pg_stat_statements 统计",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="检查是否已启用（以及缺什么）").set_defaults(fn=cmd_status)

    p_top = sub.add_parser("top", help="按总耗时列出语句")
    p_top.add_argument("--limit", type=int, default=15)
    p_top.set_defaults(fn=cmd_top)

    sub.add_parser("reset", help="清零统计").set_defaults(fn=cmd_reset)

    p_probe = sub.add_parser("probe", help="测量每次 tools/call 的 SQL 语句数")
    p_probe.add_argument("--url", default=os.environ.get("SCHEDULE_PUBLIC_URL", "http://127.0.0.1:8000"))
    p_probe.add_argument("--token", default=None)
    p_probe.add_argument("--requests", type=int, default=20)
    p_probe.add_argument("--tool", default=DEFAULT_TOOL,
                         help=f"工具名，默认 {DEFAULT_TOOL}")
    p_probe.add_argument("--arguments", default=None,
                         help='JSON 参数，默认 {}。例：\'{"keyword":"alpha"}\'')
    p_probe.set_defaults(fn=cmd_probe)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except psycopg_error_types() as exc:
        print(f"数据库错误：{str(exc).splitlines()[0]}", file=sys.stderr)
        return 1


def psycopg_error_types():
    import psycopg

    return psycopg.Error


if __name__ == "__main__":
    raise SystemExit(main())
