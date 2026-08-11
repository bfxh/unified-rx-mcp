"""Pydantic request/response models for the PR Test Oracle API."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator


class PRInfo(BaseModel):
    """Parsed PR information extracted from PR URL."""

    owner: str
    repo: str
    pr_number: int
    url: str


class AnalyzeRequest(BaseModel):
    """Request payload for /analyze endpoint."""

    pr_url: str = Field(
        description="GitHub PR URL (e.g., https://github.com/owner/repo/pull/123)"
    )
    repo_path: str | None = Field(
        default=None, description="Optional local path to the repository"
    )
    repo_url: str | None = Field(
        default=None, description="Repository URL for cloning if repo_path not provided"
    )
    ai_provider: Literal["claude", "gemini", "cursor"] | None = Field(
        default=None, description="AI provider (overrides env var)"
    )
    ai_model: str | None = Field(
        default=None, description="AI model (overrides env var)"
    )

    ai_cli_timeout: Annotated[int, Field(gt=0)] | None = Field(
        default=None, description="AI CLI timeout in minutes"
    )
    github_token: str | None = Field(
        default=None,
        description="GitHub token (overrides env var)",
        json_schema_extra={"format": "password"},
    )
    test_patterns: list[str] | None = Field(
        default=None, description="Glob patterns for test files"
    )
    post_comment: bool | None = Field(
        default=None, description="Whether to post a comment on the PR (default: true)"
    )
    raw_prompt: str | None = Field(
        default=None,
        description="Raw custom prompt text with additional AI instructions",
    )

    @field_validator("pr_url")
    @classmethod
    def validate_pr_url(cls, v: str) -> str:
        """Validate that pr_url matches the expected GitHub PR URL pattern."""
        pattern = r"^https://github\.com/[\w.\-]+/[\w.\-]+/pull/\d+$"
        if not re.match(pattern, v):
            msg = (
                f"Invalid GitHub PR URL: '{v}'. "
                "Expected format: https://github.com/owner/repo/pull/123"
            )
            raise ValueError(msg)
        return v

    def parse_pr_info(self) -> PRInfo:
        """Extract owner, repo, and PR number from the validated pr_url."""
        parts = self.pr_url.rstrip("/").split("/")
        return PRInfo(
            owner=parts[-4],
            repo=parts[-3],
            pr_number=int(parts[-1]),
            url=self.pr_url,
        )


class TestRecommendation(BaseModel):
    """A single test recommendation."""

    test_file: str = Field(description="Path to the test file")
    test_name: str = Field(
        default="(all)",
        description="Specific test name/class, or '(all)' for the entire file",
    )

    @field_validator("test_name", mode="before")
    @classmethod
    def coerce_null_test_name(cls, v: object) -> object:
        """Convert null/None test_name to '(all)'."""
        if v is None:
            return "(all)"
        return v

    reason: str = Field(description="Why this test should run")
    priority: Literal["critical", "standard"] = Field(description="Test priority")
    confidence: Literal["high", "medium", "low"] = Field(description="Confidence level")


class AnalyzeResponse(BaseModel):
    """Response from /analyze endpoint."""

    pr_url: str = Field(description="The analyzed PR URL")
    ai_provider: str = Field(default="", description="AI provider used")
    ai_model: str = Field(default="", description="AI model used")
    recommendations: list[TestRecommendation] = Field(
        default_factory=list, description="Test recommendations"
    )
    summary: str = Field(default="", description="Human-readable summary")
    review_posted: bool = Field(
        default=False, description="Whether a PR review was posted"
    )
    review_url: str | None = Field(default=None, description="URL of the posted review")


class TestMapping(BaseModel):
    """Mapping of a changed source file to candidate test files."""

    source_file: str = Field(description="Changed source file path")
    candidate_tests: list[str] = Field(
        default_factory=list, description="Related test file paths"
    )
    mapping_reason: str = Field(
        default="", description="How the mapping was determined"
    )
