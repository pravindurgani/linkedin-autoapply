"""Data models for jobs, matches, and applications."""

from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class JobSource(str, Enum):
    LINKEDIN = "linkedin"


class ApplicationStatus(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"


class ApplyMethod(str, Enum):
    EASY_APPLY = "easy_apply"
    NONE = "none"


class Job(BaseModel):
    id: Optional[int] = None
    title: str
    company: str
    location: str
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_text: Optional[str] = None
    description: str = ""
    url: str
    source: JobSource
    external_id: str
    posted_date: Optional[str] = None
    easy_apply: bool = False
    scraped_at: datetime = Field(default_factory=datetime.now)

    @property
    def salary_display(self) -> str:
        if self.salary_min and self.salary_max:
            return f"£{self.salary_min:,.0f} - £{self.salary_max:,.0f}"
        if self.salary_min:
            return f"£{self.salary_min:,.0f}+"
        if self.salary_text:
            return self.salary_text
        return "Not specified"


class MatchResult(BaseModel):
    job_id: int
    score: int = Field(ge=0, le=100)
    reasoning: str = ""
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    scored_at: datetime = Field(default_factory=datetime.now)


class Application(BaseModel):
    job_id: int
    status: ApplicationStatus = ApplicationStatus.PENDING
    method: ApplyMethod = ApplyMethod.NONE
    applied_at: Optional[datetime] = None
    error_message: Optional[str] = None
