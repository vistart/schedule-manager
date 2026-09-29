"""CLI for schedule management — LLM-optimized design.

All output is JSON by default (envelope structure). Use --human for human-readable.
All parameters are named (--key value). No interactive prompts.

Identity comes from a bearer token (``--token`` / ``SCHEDULE_TOKEN`` /
``SCHEDULE_TOKEN_FILE``).  The same credential a remote MCP client sends in the
``Authorization`` header is accepted here, so one token works on both surfaces.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Any, Optional

from pydantic import ValidationError
from rhosocial.activerecord.backend.errors import DatabaseError
from .config import configure_logging, get_token
from .db import close_pool, connection, create_pool
from .errors import (
    AccountClosed,
    ScheduleManagerError,
    Unauthenticated,
    UsernameTaken,
)
from .identity import current_cli_token, current_user_id, set_cli_user
from .models import SCOPES, ApiToken, Schedule, User

# ── Exit codes ───────────────────────────────────────────────────────────────

EXIT_OK = 0
EXIT_GENERAL = 1
EXIT_ARGS = 2
EXIT_NOT_FOUND = 20
EXIT_VALIDATION = 40
EXIT_UNAUTHENTICATED = 41
EXIT_FORBIDDEN = 42


# ── Output helpers ───────────────────────────────────────────────────────────

def _ok(data: Any) -> None:
    print(json.dumps({"status": "ok", "data": data}, ensure_ascii=False, default=str))
    sys.exit(EXIT_OK)


def _err(code: str, message: str, exit_code: int = EXIT_GENERAL) -> None:
    print(json.dumps({"status": "error", "error": {"code": code, "message": message}}, ensure_ascii=False))
    sys.exit(exit_code)


def _human(data: dict) -> str:
    lines = []
    for k, v in data.items():
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v) if v else "(empty)"
        elif isinstance(v, bool):
            v = "yes" if v else "no"
        elif v is None:
            v = "-"
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def _emit(data: dict) -> None:
    if getattr(_CURRENT_ARGS, "human", False):
        print(_human(data))
        sys.exit(EXIT_OK)
    _ok(data)


def _parse_datetime(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError) as e:
        _err("INVALID_INPUT", f"Invalid datetime '{s}': {e}", EXIT_ARGS)


def _schedule_dict(s: Schedule) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "description": s.description,
        "status": s.status,
        "priority": s.priority,
        "start_time": s.start_time.isoformat() if s.start_time else None,
        "due_time": s.due_time.isoformat() if s.due_time else None,
        "completed_at": s.completed_at.isoformat() if s.completed_at else None,
        "location": s.location,
        "tags": s.tags or [],
        "rrule": s.rrule,
        "rdate": s.rdate or [],
        "exdate": s.exdate or [],
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "is_overdue": s.is_overdue,
    }


# ── Init ─────────────────────────────────────────────────────────────────────

_CURRENT_ARGS = argparse.Namespace(human=False)
_POOL = None

#: Management commands provision and revoke credentials, so they cannot require
#: one — that is circular.  They are authorised by local access instead: whoever
#: can run this process already holds the database credentials in the
#: environment.  Never expose them over the network.
MANAGEMENT_COMMANDS = frozenset({"user", "token"})


async def _init() -> None:
    """Connect, then bind one pooled connection to this task.

    The pool has to exist before the connection can be acquired, and it is
    stored on the module because ``_run`` needs it again to close.
    """
    global _POOL
    try:
        _POOL = await create_pool()
        configure_logging()
    except DatabaseError as e:
        _err("CONFIG_ERROR", str(e), EXIT_GENERAL)
        return

    if _CURRENT_ARGS.command in MANAGEMENT_COMMANDS:
        return

    try:
        token = getattr(_CURRENT_ARGS, "token", None) or get_token()
    except DatabaseError as e:
        _err("CONFIG_ERROR", str(e), EXIT_GENERAL)
        return
    if not token:
        _err(
            "UNAUTHENTICATED",
            "no bearer token. Pass --token, or set SCHEDULE_TOKEN / "
            "SCHEDULE_TOKEN_FILE (see .env.example).",
            EXIT_UNAUTHENTICATED,
        )
    try:
        user = await User.authenticate(token)
    except Unauthenticated as e:
        _err("UNAUTHENTICATED", str(e), EXIT_UNAUTHENTICATED)
        return
    except AccountClosed as e:
        _err("FORBIDDEN", str(e), EXIT_FORBIDDEN)
        return
    set_cli_user(user.id, token)


# ── Commands ─────────────────────────────────────────────────────────────────

async def _cmd_whoami(args: argparse.Namespace) -> None:
    user = await User.find_one(current_user_id())
    token = current_cli_token()
    granted: list[str] = []
    if token:
        record = await User.resolve_token(token)
        granted = list(record.scope_list or ())
    data = {
        "user_id": user.id,
        "username": user.username,
        "is_active": user.is_active,
        "scopes": granted,
        "available_scopes": list(SCOPES),
    }
    if getattr(args, "human", False):
        print(_human(data))
        sys.exit(EXIT_OK)
    _ok(data)


async def _cmd_create(args: argparse.Namespace) -> None:
    if args.dry_run:
        _ok({"dry_run": True, "would_create": {"title": args.title, "status": args.status or "pending", "priority": args.priority or 3}})
        return
    try:
        s = Schedule(
            title=args.title,
            description=args.description,
            status=args.status or "pending",
            priority=args.priority or 3,
        )
    except ValidationError as e:
        _err("VALIDATION_ERROR", str(e), EXIT_VALIDATION)
        return
    if args.start_time:
        s.start_time = _parse_datetime(args.start_time)
    if args.due_time:
        s.due_time = _parse_datetime(args.due_time)
    if args.location:
        s.location = args.location
    if args.tags:
        s.tags = [t.strip() for t in args.tags.split(",")]
    if args.rrule:
        s.rrule = args.rrule
    try:
        await s.save()
    except ValidationError as e:
        _err("VALIDATION_ERROR", str(e), EXIT_VALIDATION)
        return
    _emit(_schedule_dict(s))


async def _cmd_get(args: argparse.Namespace) -> None:
    s = await Schedule.find_one(args.id)
    if s is None:
        _err("NOT_FOUND", f"Schedule {args.id} not found", EXIT_NOT_FOUND)
        return
    _emit(_schedule_dict(s))


async def _cmd_update(args: argparse.Namespace) -> None:
    s = await Schedule.find_one(args.id)
    if s is None:
        _err("NOT_FOUND", f"Schedule {args.id} not found", EXIT_NOT_FOUND)
        return
    if args.title is not None:
        s.title = args.title
    if args.description is not None:
        s.description = args.description
    if args.status is not None:
        s.status = args.status
    if args.priority is not None:
        s.priority = args.priority
    if args.start_time is not None:
        s.start_time = _parse_datetime(args.start_time)
    if args.due_time is not None:
        s.due_time = _parse_datetime(args.due_time)
    if args.location is not None:
        s.location = args.location
    if args.tags is not None:
        s.tags = [t.strip() for t in args.tags.split(",")]
    if args.dry_run:
        _ok({"dry_run": True, "would_update": _schedule_dict(s)})
        return
    try:
        await s.save()
    except ValidationError as e:
        _err("VALIDATION_ERROR", str(e), EXIT_VALIDATION)
        return
    _emit(_schedule_dict(s))


async def _cmd_delete(args: argparse.Namespace) -> None:
    s = await Schedule.find_one(args.id)
    if s is None:
        _err("NOT_FOUND", f"Schedule {args.id} not found", EXIT_NOT_FOUND)
        return
    if args.dry_run:
        _ok({"dry_run": True, "would_delete": {"id": s.id, "title": s.title}})
        return
    await s.delete()
    data = {"deleted": {"id": args.id}}
    if getattr(args, "human", False):
        print(_human(data))
        sys.exit(EXIT_OK)
    _ok(data)


async def _cmd_complete(args: argparse.Namespace) -> None:
    s = await Schedule.find_one(args.id)
    if s is None:
        _err("NOT_FOUND", f"Schedule {args.id} not found", EXIT_NOT_FOUND)
        return
    await s.complete()
    if args.dry_run:
        _ok({"dry_run": True, "would_complete": _schedule_dict(s)})
        return
    await s.save()
    _emit(_schedule_dict(s))


async def _cmd_list(args: argparse.Namespace) -> None:
    items, total, page, page_size = await Schedule.list_page(
        page=args.page,
        page_size=args.page_size,
        status=args.status,
        priority=args.priority,
        keyword=args.keyword,
        sort_by=args.sort_by,
        sort_order=args.sort_order,
    )
    _emit({
        "items": [_schedule_dict(s) for s in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
    })


async def _cmd_search(args: argparse.Namespace) -> None:
    items, has_more = await Schedule.search(
        args.keyword, page=args.page, page_size=args.page_size
    )
    _emit(
        {
            "items": [_schedule_dict(s) for s in items],
            "has_more": has_more,
            "page": args.page,
            "page_size": args.page_size,
        }
    )


# ── Management commands ──────────────────────────────────────────────────────


async def _cmd_user_open(args: argparse.Namespace) -> None:
    try:
        scopes = _split_scopes(args.scope) or list(SCOPES)
        user, token = await User.open_account(
            args.username, scopes=scopes, label=args.label
        )
    except UsernameTaken as e:
        _err("VALIDATION_ERROR", str(e), EXIT_VALIDATION)
        return
    except ValueError as e:
        _err("VALIDATION_ERROR", str(e), EXIT_VALIDATION)
        return
    _emit({
        "user_id": user.id,
        "username": user.username,
        "scopes": scopes,
        "token": token,
        "token_notice": "shown once; only its SHA-256 digest is stored",
    })


async def _cmd_user_close(args: argparse.Namespace) -> None:
    user = await User.find_one(args.id)
    if user is None:
        _err("NOT_FOUND", f"User {args.id} not found", EXIT_NOT_FOUND)
        return
    await user.close_account()
    _emit({"closed": {"id": user.id, "username": user.username}})


async def _cmd_token_issue(args: argparse.Namespace) -> None:
    user = await User.find_one(args.id)
    if user is None:
        _err("NOT_FOUND", f"User {args.id} not found", EXIT_NOT_FOUND)
        return
    try:
        scopes = _split_scopes(args.scope) or list(SCOPES)
        token, token_id = await user.issue_token(scopes=scopes, label=args.label)
    except ValueError as e:
        _err("VALIDATION_ERROR", str(e), EXIT_VALIDATION)
        return
    _emit({"user_id": user.id, "token_id": token_id, "token": token, "scopes": scopes})


async def _cmd_token_revoke(args: argparse.Namespace) -> None:
    record = await _find_token(args.token_id)
    if record is None:
        _err("NOT_FOUND", f"Token {args.token_id} not found", EXIT_NOT_FOUND)
        return
    user = await User.find_one(record.user_id)
    if user is None:
        _err("NOT_FOUND", f"User {record.user_id} not found", EXIT_NOT_FOUND)
        return
    await user.revoke_token(record.id)
    _emit({"revoked": {"token_id": record.id, "label": record.label}})


async def _cmd_token_list(args: argparse.Namespace) -> None:
    query = ApiToken.query()
    if args.id is not None:
        query = query.where(ApiToken.c.user_id == args.id)
    records = await query.order_by((ApiToken.c.id, "ASC")).all()
    items = [
        {
            "id": r.id,
            "user_id": r.user_id,
            "label": r.label,
            "token_hash_prefix": r.token_hash[:8],
            "revoked": r.revoked_at is not None,
            "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        }
        for r in records
    ]
    _emit({"items": items, "total": len(items)})


def _split_scopes(raw: Optional[str]) -> Optional[list[str]]:
    return [s.strip() for s in raw.split(",") if s.strip()] if raw else None


async def _find_token(token_id: int) -> Optional[ApiToken]:
    return await ApiToken.find_one(token_id)


# ── Describe ─────────────────────────────────────────────────────────────────


def _describe() -> None:
    schema = {
        "name": "schedule-manager",
        "version": "0.2.0",
        "description": "Schedule management CLI for LLM tool calling",
        "output_format": "JSON envelope: {\"status\": \"ok\"|\"error\", \"data\": ..., \"error\": ...}",
        "authentication": {
            "how": "Business commands require a bearer token via --token, SCHEDULE_TOKEN, or "
                   "SCHEDULE_TOKEN_FILE. Remote MCP clients send the same token in the "
                   "Authorization header. Management commands ('user', 'token') are exempt: they "
                   "provision and revoke credentials, and are authorised by local access to the "
                   "database rather than by a token.",
            "scope": "Tokens resolve to exactly one user. Every command sees only that user's schedules; "
                     "another user's id reads as NOT_FOUND rather than FORBIDDEN, so ids cannot be probed.",
            "whoami": "Run 'whoami' to see the current user.",
        },
        "exit_codes": {
            "0": "success",
            "1": "general error (including configuration problems)",
            "2": "argument error",
            "20": "resource not found (including another user's schedule)",
            "40": "validation error",
            "41": "unauthenticated (missing, unknown, revoked, or expired token)",
            "42": "forbidden (deactivated account)",
        },
        "global_options": {
            "--token": "Bearer token. Defaults to $SCHEDULE_TOKEN, then $SCHEDULE_TOKEN_FILE.",
            "--human": "Output human-readable text instead of JSON",
            "--describe": "Show this schema",
            "--help": "Show help",
        },
        "commands": {
            "whoami": {
                "description": "Show the authenticated user and the token's scopes",
                "params": {},
                "examples": ["schedule-manager whoami"],
            },
            "create": {
                "description": "Create a new schedule owned by the authenticated user",
                "params": {
                    "title": {"type": "string", "required": True, "description": "Schedule title (non-empty)"},
                    "description": {"type": "string", "required": False, "description": "Detailed description"},
                    "status": {"type": "string", "required": False, "default": "pending", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                    "priority": {"type": "integer", "required": False, "default": 3, "min": 1, "max": 5, "description": "1=highest, 5=lowest"},
                    "start_time": {"type": "string", "required": False, "format": "iso8601", "description": "Start datetime"},
                    "due_time": {"type": "string", "required": False, "format": "iso8601", "description": "Deadline"},
                    "location": {"type": "string", "required": False, "description": "Location"},
                    "tags": {"type": "string", "required": False, "description": "Comma-separated tags"},
                    "rrule": {"type": "string", "required": False, "description": "RFC 5545 recurrence rule"},
                },
                "examples": [
                    "schedule-manager create --title 'Team standup' --priority 3",
                    "schedule-manager create --title 'Deploy v2' --due-time '2026-09-10T14:00:00' --tags 'deploy,critical'",
                ],
            },
            "get": {
                "description": "Get one of the authenticated user's schedules by ID",
                "params": {
                    "id": {"type": "integer", "required": True, "description": "Schedule ID"},
                },
                "examples": ["schedule-manager get --id 1"],
            },
            "update": {
                "description": "Update one of the authenticated user's schedules (only provided fields are changed)",
                "params": {
                    "id": {"type": "integer", "required": True, "description": "Schedule ID"},
                    "title": {"type": "string", "required": False},
                    "description": {"type": "string", "required": False},
                    "status": {"type": "string", "required": False, "enum": ["pending", "in_progress", "completed", "cancelled"]},
                    "priority": {"type": "integer", "required": False, "min": 1, "max": 5},
                    "start_time": {"type": "string", "required": False, "format": "iso8601"},
                    "due_time": {"type": "string", "required": False, "format": "iso8601"},
                    "location": {"type": "string", "required": False},
                    "tags": {"type": "string", "required": False, "description": "Comma-separated tags"},
                },
                "examples": [
                    "schedule-manager update --id 1 --status completed",
                    "schedule-manager update --id 1 --title 'Updated title' --priority 1",
                ],
            },
            "delete": {
                "description": "Soft-delete one of the authenticated user's schedules",
                "params": {
                    "id": {"type": "integer", "required": True},
                },
                "examples": ["schedule-manager delete --id 1"],
            },
            "complete": {
                "description": "Mark one of the authenticated user's schedules as completed",
                "params": {
                    "id": {"type": "integer", "required": True},
                },
                "examples": ["schedule-manager complete --id 1"],
            },
            "list": {
                "description": "List the authenticated user's schedules with pagination, filtering, and sorting",
                "params": {
                    "page": {"type": "integer", "required": False, "default": 1, "min": 1},
                    "page_size": {"type": "integer", "required": False, "default": 20, "min": 1, "max": 100},
                    "status": {"type": "string", "required": False, "enum": ["pending", "in_progress", "completed", "cancelled"]},
                    "priority": {"type": "integer", "required": False, "min": 1, "max": 5},
                    "keyword": {"type": "string", "required": False, "description": "Search keyword (matches title and description)"},
                    "sort_by": {"type": "string", "required": False, "default": "due_time", "enum": ["due_time", "created_at", "updated_at", "priority", "title"]},
                    "sort_order": {"type": "string", "required": False, "default": "asc", "enum": ["asc", "desc"]},
                },
                "examples": [
                    "schedule-manager list",
                    "schedule-manager list --status pending --sort-by priority --sort-order asc",
                    "schedule-manager list --keyword meeting --page 2 --page-size 10",
                ],
            },
            "search": {
                "description": "Search the authenticated user's schedules by keyword (matches title and description)",
                "params": {
                    "keyword": {"type": "string", "required": True, "description": "Search term"},
                    "page": {"type": "integer", "required": False, "default": 1, "min": 1},
                    "page_size": {"type": "integer", "required": False, "default": 20, "min": 1, "max": 100},
                },
                "examples": ["schedule-manager search --keyword deadline"],
            },
            "user open": {
                "description": "Management: create an account and issue its first token. Prints the plaintext token once.",
                "params": {
                    "username": {"type": "string", "required": True},
                    "scope": {"type": "string", "required": False, "description": f"Comma-separated; one of {', '.join(SCOPES)}"},
                    "label": {"type": "string", "required": False},
                },
                "examples": ["schedule-manager user open --username alice"],
            },
            "user close": {
                "description": "Management: deactivate an account and revoke all of its tokens. Schedules are kept but become unreachable.",
                "params": {"id": {"type": "integer", "required": True}},
                "examples": ["schedule-manager user close --id 1"],
            },
            "token issue": {
                "description": "Management: mint an extra token for a user",
                "params": {
                    "id": {"type": "integer", "required": True},
                    "scope": {"type": "string", "required": False},
                    "label": {"type": "string", "required": False},
                },
                "examples": ["schedule-manager token issue --id 1 --label laptop"],
            },
            "token revoke": {
                "description": "Management: revoke a token by id",
                "params": {"token_id": {"type": "integer", "required": True}},
                "examples": ["schedule-manager token revoke --token-id 3"],
            },
            "token list": {
                "description": "Management: list tokens. Shows only the first 8 hex characters of each digest, never a token.",
                "params": {"id": {"type": "integer", "required": False, "description": "Restrict to one user"}},
                "examples": ["schedule-manager token list --id 1"],
            },
        },
    }
    print(json.dumps(schema, indent=2, ensure_ascii=False))
    sys.exit(EXIT_OK)


# ── CLI parser ───────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    dry_run_parent = argparse.ArgumentParser(add_help=False)
    dry_run_parent.add_argument("--dry-run", action="store_true", help="Preview action without executing")

    parser = argparse.ArgumentParser(
        prog="schedule-manager",
        description="Schedule management CLI for LLM tool calling. Default output is JSON. Every command requires a bearer token.",
        epilog="Examples:\n"
               "  schedule-manager whoami\n"
               "  schedule-manager create --title 'Team standup' --priority 3\n"
               "  schedule-manager list --status pending --sort-by priority\n"
               "  schedule-manager search --keyword deadline\n"
               "  schedule-manager --describe\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--human", action="store_true", help="Output human-readable text")
    parser.add_argument("--describe", action="store_true", help="Output machine-readable JSON schema and exit")
    parser.add_argument("--token", help="Bearer token. Falls back to $SCHEDULE_TOKEN then $SCHEDULE_TOKEN_FILE.")

    sub = parser.add_subparsers(dest="command")

    p_who = sub.add_parser("whoami", help="Show the authenticated user")
    p_who.set_defaults(func=_cmd_whoami)

    p_create = sub.add_parser("create", parents=[dry_run_parent], help="Create a new schedule")
    p_create.add_argument("--title", required=True, help="Schedule title (non-empty)")
    p_create.add_argument("--description", "-d", help="Detailed description")
    p_create.add_argument("--status", choices=["pending", "in_progress", "completed", "cancelled"])
    p_create.add_argument("--priority", type=int, choices=range(1, 6), help="Priority 1-5 (default: 3)")
    p_create.add_argument("--start-time", help="ISO 8601 start time")
    p_create.add_argument("--due-time", help="ISO 8601 deadline")
    p_create.add_argument("--location", help="Location")
    p_create.add_argument("--tags", help="Comma-separated tags")
    p_create.add_argument("--rrule", help="RFC 5545 recurrence rule")
    p_create.set_defaults(func=_cmd_create)

    p_get = sub.add_parser("get", help="Get a schedule by ID")
    p_get.add_argument("--id", type=int, required=True, help="Schedule ID")
    p_get.set_defaults(func=_cmd_get)

    p_update = sub.add_parser("update", parents=[dry_run_parent], help="Update a schedule (only provided fields change)")
    p_update.add_argument("--id", type=int, required=True, help="Schedule ID")
    p_update.add_argument("--title", help="New title")
    p_update.add_argument("--description", "-d", help="New description")
    p_update.add_argument("--status", choices=["pending", "in_progress", "completed", "cancelled"])
    p_update.add_argument("--priority", type=int, choices=range(1, 6))
    p_update.add_argument("--start-time", help="New ISO 8601 start time")
    p_update.add_argument("--due-time", help="New ISO 8601 deadline")
    p_update.add_argument("--location", help="New location")
    p_update.add_argument("--tags", help="New comma-separated tags")
    p_update.set_defaults(func=_cmd_update)

    p_delete = sub.add_parser("delete", parents=[dry_run_parent], help="Soft-delete a schedule")
    p_delete.add_argument("--id", type=int, required=True, help="Schedule ID")
    p_delete.set_defaults(func=_cmd_delete)

    p_complete = sub.add_parser("complete", parents=[dry_run_parent], help="Mark a schedule as completed")
    p_complete.add_argument("--id", type=int, required=True, help="Schedule ID")
    p_complete.set_defaults(func=_cmd_complete)

    p_list = sub.add_parser("list", help="List schedules with pagination, filtering, sorting")
    p_list.add_argument("--page", type=int, default=1, help="Page number (default: 1)")
    p_list.add_argument("--page-size", type=int, default=20, help="Items per page (default: 20)")
    p_list.add_argument("--status", choices=["pending", "in_progress", "completed", "cancelled"])
    p_list.add_argument("--priority", type=int, choices=range(1, 6))
    p_list.add_argument("--keyword", "-k", help="Search keyword")
    p_list.add_argument("--sort-by", default="due_time", choices=["due_time", "created_at", "updated_at", "priority", "title"])
    p_list.add_argument("--sort-order", default="asc", choices=["asc", "desc"])
    p_list.set_defaults(func=_cmd_list)

    p_search = sub.add_parser("search", help="Search schedules by keyword")
    p_search.add_argument("--keyword", "-k", required=True, help="Search term")
    p_search.add_argument("--page", type=int, default=1, help="Page number (1-based)")
    p_search.add_argument(
        "--page-size", type=int, default=20, help="Items per page (max 100)"
    )
    p_search.set_defaults(func=_cmd_search)

    p_user = sub.add_parser("user", help="Management: account lifecycle")
    user_sub = p_user.add_subparsers(dest="user_command")

    p_uopen = user_sub.add_parser("open", help="Create an account and issue its first token")
    p_uopen.add_argument("--username", required=True)
    p_uopen.add_argument("--scope", help=f"Comma-separated; one of {', '.join(SCOPES)}")
    p_uopen.add_argument("--label")
    p_uopen.set_defaults(func=_cmd_user_open)

    p_uclose = user_sub.add_parser("close", help="Deactivate an account and revoke its tokens")
    p_uclose.add_argument("--id", type=int, required=True)
    p_uclose.set_defaults(func=_cmd_user_close)

    p_token = sub.add_parser("token", help="Management: token lifecycle")
    token_sub = p_token.add_subparsers(dest="token_command")

    p_tissue = token_sub.add_parser("issue", help="Mint an extra token")
    p_tissue.add_argument("--id", type=int, required=True, help="User ID")
    p_tissue.add_argument("--scope", help=f"Comma-separated; one of {', '.join(SCOPES)}")
    p_tissue.add_argument("--label")
    p_tissue.set_defaults(func=_cmd_token_issue)

    p_trevoke = token_sub.add_parser("revoke", help="Revoke a token")
    p_trevoke.add_argument("--token-id", type=int, required=True)
    p_trevoke.set_defaults(func=_cmd_token_revoke)

    p_tlist = token_sub.add_parser("list", help="List tokens (digests only)")
    p_tlist.add_argument("--id", type=int, help="Restrict to one user")
    p_tlist.set_defaults(func=_cmd_token_list)

    return parser


# ── Main ─────────────────────────────────────────────────────────────────────

async def _run(args: argparse.Namespace) -> None:
    if not args.command:
        _err("MISSING_COMMAND", "No command specified. Use --help for usage.", EXIT_ARGS)
    if args.command in MANAGEMENT_COMMANDS:
        attr = "user_command" if args.command == "user" else "token_command"
        if not getattr(args, attr, None):
            _err(
                "MISSING_COMMAND",
                f"'{args.command}' needs a subcommand. Use --help.",
                EXIT_ARGS,
            )
    global _CURRENT_ARGS
    _CURRENT_ARGS = args
    await _init()
    try:
        # One connection for the whole command: every model resolves to it, and
        # the command's transactions all live inside this scope.
        async with connection(_POOL):
            await args.func(args)
    finally:
        await close_pool(_POOL)


def main() -> None:
    # A CLI process is short-lived and uses one connection, so pool warmup
    # would only cost an extra connect.
    os.environ.setdefault("SCHEDULE_POOL_MIN", "0")
    # Before anything else: the ORM logs every statement at DEBUG, and those
    # lines would land on the same stream as the JSON envelope.
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.describe:
            _describe()
            return
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        sys.exit(EXIT_GENERAL)
    except SystemExit:
        raise
    except ScheduleManagerError as exc:
        _err("SCHEDULE_MANAGER_ERROR", str(exc), EXIT_GENERAL)
    except DatabaseError as exc:
        _err("CONFIG_ERROR", str(exc), EXIT_GENERAL)
    except Exception as exc:  # noqa: BLE001 - the envelope is the contract here
        # A traceback on stdout would break every caller that parses JSON.
        print(f"unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        _err("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", EXIT_GENERAL)


if __name__ == "__main__":
    main()
