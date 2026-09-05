"""CLI for schedule management — LLM-optimized design.

All output is JSON by default (envelope structure). Use --human for human-readable.
All parameters are named (--key value). No interactive prompts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from typing import Any, Optional

from pydantic import ValidationError

from rhosocial.activerecord.backend.impl.postgres import AsyncPostgresBackend

from .config import get_db_config
from .model import Schedule

# ── Exit codes ───────────────────────────────────────────────────────────────

EXIT_OK = 0
EXIT_GENERAL = 1
EXIT_ARGS = 2
EXIT_NOT_FOUND = 20
EXIT_VALIDATION = 40


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

async def _init() -> None:
    config = get_db_config()
    await Schedule.configure(config, AsyncPostgresBackend)


# ── Commands ─────────────────────────────────────────────────────────────────

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
    data = _schedule_dict(s)
    if getattr(args, "human", False):
        print(_human(data))
        sys.exit(EXIT_OK)
    _ok(data)


async def _cmd_get(args: argparse.Namespace) -> None:
    s = await Schedule.find_one(args.id)
    if s is None:
        _err("NOT_FOUND", f"Schedule {args.id} not found", EXIT_NOT_FOUND)
        return
    data = _schedule_dict(s)
    if getattr(args, "human", False):
        print(_human(data))
        sys.exit(EXIT_OK)
    _ok(data)


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
    data = _schedule_dict(s)
    if getattr(args, "human", False):
        print(_human(data))
        sys.exit(EXIT_OK)
    _ok(data)


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
    data = _schedule_dict(s)
    if getattr(args, "human", False):
        print(_human(data))
        sys.exit(EXIT_OK)
    _ok(data)


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
    result = {
        "items": [_schedule_dict(s) for s in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
    }
    if getattr(args, "human", False):
        print(_human(result))
        sys.exit(EXIT_OK)
    _ok(result)


async def _cmd_search(args: argparse.Namespace) -> None:
    items = await Schedule.search(args.keyword)
    result = {
        "items": [_schedule_dict(s) for s in items],
        "total": len(items),
    }
    if getattr(args, "human", False):
        print(_human(result))
        sys.exit(EXIT_OK)
    _ok(result)


# ── Describe ─────────────────────────────────────────────────────────────────

def _describe() -> None:
    schema = {
        "name": "schedule-manager",
        "version": "0.1.0",
        "description": "Schedule management CLI for LLM tool calling",
        "output_format": "JSON envelope: {\"status\": \"ok\"|\"error\", \"data\": ..., \"error\": ...}",
        "exit_codes": {
            "0": "success",
            "1": "general error",
            "2": "argument error",
            "20": "resource not found",
            "40": "validation error",
        },
        "global_options": {
            "--human": "Output human-readable text instead of JSON",
            "--describe": "Show this schema",
            "--help": "Show help",
        },
        "commands": {
            "create": {
                "description": "Create a new schedule",
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
                "description": "Get a schedule by ID",
                "params": {
                    "id": {"type": "integer", "required": True, "description": "Schedule ID"},
                },
                "examples": ["schedule-manager get --id 1"],
            },
            "update": {
                "description": "Update an existing schedule (only provided fields are changed)",
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
                "description": "Soft-delete a schedule",
                "params": {
                    "id": {"type": "integer", "required": True},
                },
                "examples": ["schedule-manager delete --id 1"],
            },
            "complete": {
                "description": "Mark a schedule as completed",
                "params": {
                    "id": {"type": "integer", "required": True},
                },
                "examples": ["schedule-manager complete --id 1"],
            },
            "list": {
                "description": "List schedules with pagination, filtering, and sorting",
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
                "description": "Search schedules by keyword (matches title and description)",
                "params": {
                    "keyword": {"type": "string", "required": True, "description": "Search term"},
                },
                "examples": ["schedule-manager search --keyword deadline"],
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
        description="Schedule management CLI for LLM tool calling. Default output is JSON.",
        epilog="Examples:\n"
               "  schedule-manager create --title 'Team standup' --priority 3\n"
               "  schedule-manager list --status pending --sort-by priority\n"
               "  schedule-manager search --keyword deadline\n"
               "  schedule-manager --describe\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--human", action="store_true", help="Output human-readable text")
    parser.add_argument("--describe", action="store_true", help="Output machine-readable JSON schema and exit")

    sub = parser.add_subparsers(dest="command")

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
    p_search.set_defaults(func=_cmd_search)

    return parser


# ── Main ─────────────────────────────────────────────────────────────────────

async def _run(args: argparse.Namespace) -> None:
    if not args.command:
        _err("MISSING_COMMAND", "No command specified. Use --help for usage.", EXIT_ARGS)
    await _init()
    await args.func(args)


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.describe:
            _describe()
            return
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        sys.exit(EXIT_GENERAL)


if __name__ == "__main__":
    main()
