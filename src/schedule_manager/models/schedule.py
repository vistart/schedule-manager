"""Schedule model."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar, List, Optional

from dateutil.rrule import rrulestr
from dateutil.tz import tzutc
from pydantic import Field, field_validator, model_validator

from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.field.soft_delete import DefaultAsyncSoftDeleteMixin
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import AsyncActiveRecord

from .base import JsonbListMixin, UserOwnedMixin, UserScopedQuery

VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}

#: Ceiling on any single page.  The MCP tool schema is derived from type hints,
#: so there is nothing upstream stopping a caller from asking for a million
#: rows; this clamp is the only backstop.
MAX_PAGE_SIZE = 100

_JSON_LIST_FIELDS = ("tags", "rdate", "exdate")


class Schedule(
    UserOwnedMixin,
    JsonbListMixin,
    DefaultTimestampMixin,
    DefaultAsyncSoftDeleteMixin,
    AsyncActiveRecord,
):
    """A task or event, owned by exactly one user.

    ``__query_class__`` is what makes the isolation total: every read path —
    ``find_one``, ``find_all``, ``list_page``, ``search``, and each terminal
    method on the query object — is forced through the current identity.  A
    caller cannot reach another user's row by passing a different id.
    """

    __table_name__ = "schedules"
    __pk_auto_generated__ = True
    __query_class__ = UserScopedQuery
    __jsonb_list_fields__ = _JSON_LIST_FIELDS

    c: ClassVar[FieldProxy] = FieldProxy()

    id: Optional[int] = None
    #: Placeholder only — ``UserOwnedMixin.prepare_save_data`` overwrites it on
    #: every write, so the value a caller supplies is never what gets stored.
    user_id: int = 0
    title: str = ""
    description: Optional[str] = None
    status: str = "pending"
    priority: int = 3
    start_time: Optional[datetime] = None
    due_time: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    location: Optional[str] = None
    tags: Optional[list] = Field(default_factory=list)
    rrule: Optional[str] = None
    rdate: Optional[list] = Field(default_factory=list)
    exdate: Optional[list] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def title_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title must not be empty")
        return v.strip()

    @field_validator("status")
    @classmethod
    def valid_status(cls, v: str) -> str:
        if v not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        return v

    @field_validator("priority")
    @classmethod
    def valid_priority(cls, v: int) -> int:
        if not 1 <= v <= 5:
            raise ValueError("priority must be between 1 and 5")
        return v

    @field_validator("rrule")
    @classmethod
    def valid_rrule(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            rrulestr(v)
        except (ValueError, TypeError) as e:
            raise ValueError(f"invalid RRULE string: {e}") from e
        return v

    @model_validator(mode="after")
    def validate_timing(self) -> "Schedule":
        if self.start_time and self.due_time and self.start_time > self.due_time:
            raise ValueError("start_time must be before due_time")
        return self

    async def complete(self) -> None:
        self.status = "completed"
        self.completed_at = datetime.now(tzutc())

    async def reopen(self) -> None:
        self.status = "pending"
        self.completed_at = None

    @property
    def is_overdue(self) -> bool:
        if self.due_time is None:
            return False
        if self.status in ("completed", "cancelled"):
            return False
        now = datetime.now(tzutc())
        due = self.due_time if self.due_time.tzinfo else self.due_time.replace(tzinfo=tzutc())
        return due < now

    def next_occurrence(self, from_dt: Optional[datetime] = None) -> Optional[datetime]:
        if self.rrule is None:
            return None
        if from_dt is None:
            from_dt = datetime.now(tzutc())
        try:
            dtstart = self.start_time
            if dtstart is not None and dtstart.tzinfo is None:
                dtstart = dtstart.replace(tzinfo=tzutc())
            rule = rrulestr(self.rrule, dtstart=dtstart)
            after = from_dt if from_dt.tzinfo else from_dt.replace(tzinfo=tzutc())
            return rule.after(after, inc=False)
        except (ValueError, TypeError):
            return None

    @classmethod
    async def search(
        cls,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[List["Schedule"], bool]:
        """Search the caller's schedules, newest update first.

        Returns ``(items, has_more)`` rather than a bare list.  A limit without
        a "there is more" signal is worse than no limit for an LLM client: the
        caller cannot tell 20 matches from 200, and silently reports an
        incomplete result as complete.

        ``has_more`` is derived by fetching one extra row instead of running a
        ``COUNT(*)``, which keeps this to a single statement -- the same reason
        ``list_page``'s count is the expensive part of a listing.
        """
        page = max(1, page)
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        kw = f"%{keyword}%"
        rows = list(
            await (
                cls.query()
                .where((Schedule.c.title.ilike(kw)) | (Schedule.c.description.ilike(kw)))
                .order_by((Schedule.c.updated_at, "DESC"))
                .limit(page_size + 1)
                .offset((page - 1) * page_size)
                .all()
            )
        )
        return rows[:page_size], len(rows) > page_size

    @classmethod
    async def list_page(
        cls,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None,
        priority: Optional[int] = None,
        keyword: Optional[str] = None,
        sort_by: str = "due_time",
        sort_order: str = "asc",
    ) -> tuple[List["Schedule"], int, int, int]:
        q = cls.query()

        if status is not None:
            q = q.where(Schedule.c.status == status)
        if priority is not None:
            q = q.where(Schedule.c.priority == priority)
        if keyword:
            kw = f"%{keyword}%"
            q = q.where(
                (Schedule.c.title.ilike(kw)) | (Schedule.c.description.ilike(kw))
            )

        total = await q.count()

        col = getattr(Schedule.c, sort_by, Schedule.c.due_time)
        direction = "DESC" if sort_order == "desc" else "ASC"
        q = q.order_by((col, direction))

        offset = (page - 1) * page_size
        items = list(await q.limit(page_size).offset(offset).all())

        return items, total, page, page_size

    @classmethod
    async def overdue(cls) -> List["Schedule"]:
        now = datetime.now(tzutc())
        results = await (
            cls.query()
            .where(
                (Schedule.c.due_time < now)
                & (Schedule.c.status.not_in(["completed", "cancelled"]))
                & (Schedule.c.due_time.is_not_null())
            )
            .order_by((Schedule.c.due_time, "ASC"))
            .all()
        )
        return list(results)
