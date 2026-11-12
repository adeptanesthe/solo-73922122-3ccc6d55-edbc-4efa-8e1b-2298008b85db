"""Pydantic request/response models for the filing API."""

from __future__ import annotations

import re
from typing import Annotated, List, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# Lifecycle states of a filing request.
#   accepted  -> stored, waiting for / being processed by the worker
#   running   -> the single worker is actively driving the browser
#   success   -> portal submission confirmed and receipt saved
#   conflict  -> portal already holds different data for the month
#   failed    -> processing error (browser/portal unreachable, etc.)
TASK_STATES = ("accepted", "running", "success", "conflict", "failed")

LineId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^\S+$")]
Kilometres = Annotated[int, Field(ge=0, le=10_000_000, strict=True)]


class LineMileage(BaseModel):
    """One line entry: a unique line ID with non-negative integer kilometres."""

    model_config = ConfigDict(extra="forbid")

    line_id: LineId
    kilometres: Kilometres


class FilingRequest(BaseModel):
    """Proxy-filing request: one month with 1..20 unique line entries."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    month: str = Field(
        ...,
        description="报送月份，格式 YYYY-MM（例如 2026-11）。",
    )
    lines: List[LineMileage] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="1～20 条线路里程；line_id 必须唯一，公里数为非负整数。",
    )

    @field_validator("month")
    @classmethod
    def _check_month(cls, value: str) -> str:
        if not MONTH_PATTERN.match(value):
            raise ValueError("month 必须为 YYYY-MM 格式且为有效月份（01～12）")
        return value

    @field_validator("lines")
    @classmethod
    def _check_unique_lines(cls, value: List[LineMileage]) -> List[LineMileage]:
        ids = [item.line_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("lines 中的 line_id 必须唯一")
        return value

    def content_map(self) -> dict[str, int]:
        """Canonical line_id -> kilometres mapping for equality checks."""

        return {item.line_id: item.kilometres for item in self.lines}


class TaskOutcome(BaseModel):
    """Status view of one filing request."""

    task_id: str
    month: str
    state: Literal["accepted", "running", "success", "conflict", "failed"]
    lines: List[LineMileage]
    content_hash: str
    attempt_count: int
    receipt: dict | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class ResumeResponse(BaseModel):
    """Result of an explicit resume call."""

    task_id: str
    month: str
    state: Literal["success", "conflict", "failed", "running"]
    message: str
    receipt: dict | None = None
    error: str | None = None
