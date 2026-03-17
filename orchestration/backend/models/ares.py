#  Orchestration Engine - Ares Security Review Models
#
#  Pydantic models for pre-plan-approval security findings.
#
#  Depends on: models/enums.py
#  Used by:    services/ares.py, routes/projects.py

from typing import Literal

from pydantic import BaseModel, Field

from backend.models.enums import SecuritySeverity


class SecurityFinding(BaseModel):
    id: str
    task_index: int
    task_title: str
    category: Literal[
        "missing_auth",
        "no_input_validation",
        "exposed_secret",
        "no_tls",
        "contract_violation",
        "other",
    ]
    severity: SecuritySeverity
    description: str
    recommended_mitigation: str
    affected_files: list[str] = Field(default_factory=list)


class AresReviewResult(BaseModel):
    plan_id: str
    findings: list[SecurityFinding] = Field(default_factory=list)
    critical_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    blocked: bool = False
    reviewed_at: float
