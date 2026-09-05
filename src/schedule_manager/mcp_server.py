"""MCP server exposing schedule management tools."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator, Optional

from dateutil.tz import tzutc
from mcp.server.fastmcp import FastMCP

from rhosocial.activerecord.backend.impl.postgres import AsyncPostgresBackend

from .config import get_db_config
from .model import Schedule


def _schedule_to_dict(s: Schedule) -> dict:
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


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    config = get_db_config()
    await Schedule.configure(config, AsyncPostgresBackend)
    backend = Schedule.backend()
    expr = Schedule.generate_create_table(if_not_exists=True)
    sql, params = expr.to_sql()
    await backend.execute(sql, params)
    yield {}


mcp = FastMCP(
    "schedule-manager",
    instructions="Schedule management system. Create, read, update, delete, and search schedules with pagination, filtering, and sorting.",
    lifespan=lifespan,
)


@mcp.tool()
async def create_schedule(
    title: str,
    description: Optional[str] = None,
    status: str = "pending",
    priority: int = 3,
    start_time: Optional[str] = None,
    due_time: Optional[str] = None,
    location: Optional[str] = None,
    tags: Optional[list] = None,
    rrule: Optional[str] = None,
) -> dict:
    """Create a new schedule.

    Args:
        title: Schedule title (required, non-empty).
        description: Detailed description of the schedule.
        status: Initial status (pending, in_progress, completed, cancelled). Default: pending.
        priority: Priority level 1 (highest) to 5 (lowest). Default: 3.
        start_time: ISO 8601 datetime for when the schedule starts.
        due_time: ISO 8601 datetime for the deadline.
        location: Physical or virtual location.
        tags: List of tag strings.
        rrule: RFC 5545 recurrence rule string (e.g., "FREQ=WEEKLY;BYDAY=MO").

    Returns:
        The created schedule as a dictionary.
    """
    s = Schedule(title=title, description=description, status=status, priority=priority)
    if start_time:
        s.start_time = datetime.fromisoformat(start_time)
    if due_time:
        s.due_time = datetime.fromisoformat(due_time)
    if location:
        s.location = location
    if tags:
        s.tags = tags
    if rrule:
        s.rrule = rrule
    await s.save()
    return _schedule_to_dict(s)


@mcp.tool()
async def get_schedule(schedule_id: int) -> dict:
    """Get a schedule by its ID.

    Args:
        schedule_id: The numeric ID of the schedule.

    Returns:
        The schedule as a dictionary, or error message if not found.
    """
    s = await Schedule.find_one(schedule_id)
    if s is None:
        return {"error": f"Schedule {schedule_id} not found"}
    return _schedule_to_dict(s)


@mcp.tool()
async def update_schedule(
    schedule_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[int] = None,
    start_time: Optional[str] = None,
    due_time: Optional[str] = None,
    location: Optional[str] = None,
    tags: Optional[list] = None,
) -> dict:
    """Update an existing schedule. Only provided fields are changed.

    Args:
        schedule_id: The numeric ID of the schedule to update.
        title: New title.
        description: New description.
        status: New status (pending, in_progress, completed, cancelled).
        priority: New priority level 1-5.
        start_time: New ISO 8601 start time.
        due_time: New ISO 8601 due time.
        location: New location.
        tags: New list of tags.

    Returns:
        The updated schedule as a dictionary, or error message if not found.
    """
    s = await Schedule.find_one(schedule_id)
    if s is None:
        return {"error": f"Schedule {schedule_id} not found"}
    if title is not None:
        s.title = title
    if description is not None:
        s.description = description
    if status is not None:
        s.status = status
    if priority is not None:
        s.priority = priority
    if start_time is not None:
        s.start_time = datetime.fromisoformat(start_time)
    if due_time is not None:
        s.due_time = datetime.fromisoformat(due_time)
    if location is not None:
        s.location = location
    if tags is not None:
        s.tags = tags
    await s.save()
    return _schedule_to_dict(s)


@mcp.tool()
async def delete_schedule(schedule_id: int) -> dict:
    """Soft-delete a schedule.

    Args:
        schedule_id: The numeric ID of the schedule to delete.

    Returns:
        Confirmation message or error.
    """
    s = await Schedule.find_one(schedule_id)
    if s is None:
        return {"error": f"Schedule {schedule_id} not found"}
    await s.delete()
    return {"message": f"Schedule {schedule_id} deleted"}


@mcp.tool()
async def list_schedules(
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None,
    priority: Optional[int] = None,
    keyword: Optional[str] = None,
    sort_by: str = "due_time",
    sort_order: str = "asc",
) -> dict:
    """List schedules with pagination, filtering, and sorting.

    Args:
        page: Page number (1-based). Default: 1.
        page_size: Number of items per page. Default: 20.
        status: Filter by status (pending, in_progress, completed, cancelled).
        priority: Filter by priority level (1-5).
        keyword: Search keyword (matches title and description).
        sort_by: Field to sort by (due_time, created_at, updated_at, priority, title). Default: due_time.
        sort_order: Sort direction (asc or desc). Default: asc.

    Returns:
        Dictionary with items, total count, current page, and page size.
    """
    items, total, page, page_size = await Schedule.list_page(
        page=page,
        page_size=page_size,
        status=status,
        priority=priority,
        keyword=keyword,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return {
        "items": [_schedule_to_dict(s) for s in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
    }


@mcp.tool()
async def complete_schedule(schedule_id: int) -> dict:
    """Mark a schedule as completed.

    Args:
        schedule_id: The numeric ID of the schedule to complete.

    Returns:
        The updated schedule, or error message if not found.
    """
    s = await Schedule.find_one(schedule_id)
    if s is None:
        return {"error": f"Schedule {schedule_id} not found"}
    await s.complete()
    await s.save()
    return _schedule_to_dict(s)


@mcp.tool()
async def search_schedules(keyword: str) -> dict:
    """Search schedules by keyword (matches title and description).

    Args:
        keyword: Search term to match against title and description.

    Returns:
        List of matching schedules.
    """
    items = await Schedule.search(keyword)
    return {"items": [_schedule_to_dict(s) for s in items], "total": len(items)}
