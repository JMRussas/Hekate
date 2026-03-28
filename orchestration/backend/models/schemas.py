#  Orchestration Engine - Pydantic Schemas
#
#  Request/response models for the REST API.
#
#  Depends on: models/enums.py
#  Used by:    routes/*

import os

from pydantic import BaseModel, EmailStr, Field, field_validator

from backend.models.enums import (
    ConfidenceLevel,
    ModelTier,
    PlanningRigor,
    PlanStatus,
    ProjectStatus,
    ReassessmentOutcome,
    ResourceStatus,
    TaskStatus,
    TaskType,
)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

def _validate_repo_path(v: str | None) -> str | None:
    """Validate repo_path: must be absolute, no traversal components."""
    if v is None:
        return v
    # Check for '..' on raw input BEFORE normpath resolves it away
    raw_parts = v.replace("\\", "/").split("/")
    if ".." in raw_parts:
        raise ValueError("repo_path must not contain '..' components")
    normalized = os.path.normpath(v)
    if not os.path.isabs(normalized):
        raise ValueError("repo_path must be an absolute path")
    return normalized


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    requirements: str = Field(..., min_length=1, max_length=50_000)
    config: dict = Field(default_factory=dict)
    planning_rigor: PlanningRigor = PlanningRigor.L2
    repo_path: str | None = None
    git_base_branch: str | None = None

    @field_validator("repo_path")
    @classmethod
    def check_repo_path(cls, v):
        return _validate_repo_path(v)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    requirements: str | None = Field(default=None, min_length=1, max_length=50_000)
    config: dict | None = None
    planning_rigor: PlanningRigor | None = None
    repo_path: str | None = None
    git_base_branch: str | None = None

    @field_validator("repo_path")
    @classmethod
    def check_repo_path(cls, v):
        return _validate_repo_path(v)


class ProjectOut(BaseModel):
    id: str
    name: str
    requirements: str
    status: ProjectStatus
    created_at: float
    updated_at: float
    completed_at: float | None = None
    config: dict = Field(default_factory=dict)
    planning_rigor: str = "L2"
    task_summary: dict | None = None  # {total, completed, running, failed}
    repo_path: str | None = None
    git_base_branch: str | None = None
    git_project_branch: str | None = None
    git_state: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

class PlanOut(BaseModel):
    id: str
    project_id: str
    version: int
    model_used: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    plan: dict  # The structured plan JSON
    status: PlanStatus
    node_mapping_json: str | None = None
    created_at: float


class PlanCommentCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=10_000)


class PlanCommentOut(BaseModel):
    id: str
    plan_id: str
    project_id: str
    author: str
    content: str
    created_at: float


class PlanReviewRequest(BaseModel):
    """Request to re-review or deepen a plan with optional guidance."""
    target_rigor: PlanningRigor | None = None  # None = same rigor, set = deepen/change


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

class DependencyInfo(BaseModel):
    """Dependency detail: which task we depend on and its current status."""
    task_id: str
    title: str
    status: TaskStatus


class TaskOut(BaseModel):
    id: str
    project_id: str
    plan_id: str
    title: str
    description: str
    task_type: TaskType
    priority: int
    status: TaskStatus
    model_tier: ModelTier
    model_used: str | None = None
    wave: int = 0
    phase: str | None = None
    tools: list[str] = Field(default_factory=list)
    verification_status: str | None = None
    verification_notes: str | None = None
    requirement_ids: list[str] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    output_text: str | None = None
    output_artifacts: list[dict] = Field(default_factory=list)
    context: list[dict] = Field(default_factory=list)
    error: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    dependency_details: list[DependencyInfo] = Field(default_factory=list)
    rationale: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    git_branch: str | None = None
    git_commit_sha: str | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=50_000)
    model_tier: ModelTier | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    max_tokens: int | None = Field(default=None, ge=1, le=16384)


class ReviewAction(BaseModel):
    """User response to a NEEDS_REVIEW task."""
    action: str = Field(..., pattern="^(approve|retry)$")
    feedback: str = Field(default="", max_length=10_000)


class VerifyAction(BaseModel):
    """Verification verdict submitted by an agent (e.g. Mimir agent)."""
    verdict: str = Field(..., pattern="^(passed|gaps_found|human_needed)$")
    feedback: str = Field(default="", max_length=10_000)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class BulkTaskAction(BaseModel):
    """Perform an action on multiple tasks at once."""
    action: str = Field(..., pattern="^(retry|cancel)$")
    task_ids: list[str] = Field(..., min_length=1, max_length=100)


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

class CheckpointOut(BaseModel):
    id: str
    project_id: str
    task_id: str | None = None
    checkpoint_type: str
    summary: str
    attempts: list[dict] = Field(default_factory=list)
    question: str
    response: str | None = None
    resolved_at: float | None = None
    created_at: float


class CheckpointResolve(BaseModel):
    """User response to a checkpoint."""
    action: str = Field(..., pattern="^(retry|skip|fail)$")
    guidance: str = Field(default="", max_length=10_000)
    gotcha: str = Field(default="", max_length=2_000)


# ---------------------------------------------------------------------------
# Project Knowledge
# ---------------------------------------------------------------------------

class FindingOut(BaseModel):
    id: str
    project_id: str
    task_id: str | None = None
    category: str
    content: str
    rationale: str | None = None
    alternatives_considered: str | None = ""
    confidence: float | str = Field(default=ConfidenceLevel.MEDIUM)
    source_task_title: str | None = None
    created_at: float


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

class UsageSummary(BaseModel):
    total_cost_usd: float
    total_prompt_tokens: int
    total_completion_tokens: int
    api_call_count: int
    by_model: dict = Field(default_factory=dict)
    by_provider: dict = Field(default_factory=dict)


class BudgetStatus(BaseModel):
    daily_spent_usd: float
    daily_limit_usd: float
    daily_pct: float
    monthly_spent_usd: float
    monthly_limit_usd: float
    monthly_pct: float


class ProviderQuotaStatus(BaseModel):
    """Current quota utilization for a single provider."""
    provider: str
    utilization_pct: float
    warning: bool
    tokens_or_requests: str  # "tokens" or "requests"
    used: int
    limit: int
    remaining: int
    window_type: str  # "5h_sliding", "weekly", "daily", "60_requests_per_minute", etc.
    window_reset_at: str | None = None  # ISO 8601 timestamp


class ProviderQuotasStatus(BaseModel):
    """All provider quota statuses."""
    providers: dict[str, ProviderQuotaStatus]


# ---------------------------------------------------------------------------
# Services / Resources
# ---------------------------------------------------------------------------

class ResourceOut(BaseModel):
    id: str
    name: str
    status: ResourceStatus
    method: str = ""
    details: dict = Field(default_factory=dict)
    category: str = "ai"


class ModelOut(BaseModel):
    """A single discovered model."""
    id: str
    provider: str


class ProviderModelsOut(BaseModel):
    """Discovered models for a single provider with availability status."""
    provider: str
    models: list[ModelOut]
    available: bool
    cached_at: float
    cache_age_seconds: float | None = None
    error: str | None = None


class CliAvailabilityOut(BaseModel):
    """CLI tool availability status."""
    name: str
    tier: str
    available: bool
    path: str | None = None


class ModelsDiscoveryOut(BaseModel):
    """Complete discovery state: all providers, models, and CLI availability."""
    providers: list[ProviderModelsOut]
    cli_tools: list[CliAvailabilityOut]
    last_discovery_at: float | None = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    display_name: str = ""


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    has_password: bool = True
    linked_providers: list[str] = Field(default_factory=list)


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


# ---------------------------------------------------------------------------
# OIDC
# ---------------------------------------------------------------------------

class OIDCCallbackRequest(BaseModel):
    """OIDC authorization code callback."""
    code: str = Field(..., min_length=1)
    state: str = Field(..., min_length=1)
    state_token: str = Field(..., min_length=1)
    redirect_uri: str = Field(..., min_length=1)


class OIDCLinkRequest(BaseModel):
    """Link an OIDC provider to an existing account."""
    code: str = Field(..., min_length=1)
    state: str = Field(..., min_length=1)
    state_token: str = Field(..., min_length=1)
    redirect_uri: str = Field(..., min_length=1)


class OIDCProviderInfo(BaseModel):
    """Public info about a configured OIDC provider."""
    name: str
    display_name: str


class OIDCIdentityOut(BaseModel):
    """A linked OIDC identity for a user."""
    provider: str
    provider_email: str | None = None
    created_at: float


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

class AdminUserOut(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    is_active: bool
    created_at: float
    last_login_at: float | None = None
    project_count: int = 0


class AdminUserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|user)$")
    is_active: bool | None = None


class AdminStats(BaseModel):
    total_users: int
    active_users: int
    total_projects: int
    projects_by_status: dict[str, int]
    total_tasks: int
    tasks_by_status: dict[str, int]
    total_spend_usd: float
    spend_by_model: dict[str, float]
    task_completion_rate: float


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

class CostByProject(BaseModel):
    project_id: str
    project_name: str
    cost_usd: float
    task_count: int


class CostByModelTier(BaseModel):
    model_tier: str
    cost_usd: float
    task_count: int
    avg_cost_per_task: float


class DailyCostTrend(BaseModel):
    date: str
    cost_usd: float
    api_calls: int


class AnalyticsCostBreakdown(BaseModel):
    by_project: list[CostByProject]
    by_model_tier: list[CostByModelTier]
    daily_trend: list[DailyCostTrend]
    total_cost_usd: float


class TaskOutcomeByTier(BaseModel):
    model_tier: str
    total: int
    completed: int
    failed: int
    needs_review: int
    success_rate: float


class VerificationByTier(BaseModel):
    model_tier: str
    total_verified: int
    passed: int
    gaps_found: int
    human_needed: int
    pass_rate: float


class AnalyticsTaskOutcomes(BaseModel):
    by_tier: list[TaskOutcomeByTier]
    verification_by_tier: list[VerificationByTier]


class RetryByTier(BaseModel):
    model_tier: str
    total_tasks: int
    tasks_with_retries: int
    total_retries: int
    retry_rate: float


class WaveThroughput(BaseModel):
    project_id: str
    project_name: str
    wave: int
    task_count: int
    avg_duration_seconds: float | None = None


class CostEfficiencyItem(BaseModel):
    model_tier: str
    cost_usd: float
    tasks_completed: int
    verification_pass_count: int
    cost_per_pass: float | None = None


class AnalyticsEfficiency(BaseModel):
    retries_by_tier: list[RetryByTier]
    checkpoint_count: int
    unresolved_checkpoint_count: int
    wave_throughput: list[WaveThroughput]
    cost_efficiency: list[CostEfficiencyItem]


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class ApiKeyOut(BaseModel):
    id: str
    key_prefix: str
    name: str
    is_active: bool
    created_at: float
    last_used_at: float | None = None


class ApiKeyCreated(BaseModel):
    """Returned only on creation — includes the full key (shown once)."""
    id: str
    key: str
    key_prefix: str
    name: str
    created_at: float


# ---------------------------------------------------------------------------
# External Execution
# ---------------------------------------------------------------------------

class TaskClaimResponse(BaseModel):
    id: str
    project_id: str
    title: str
    description: str
    task_type: str
    model_tier: str
    wave: int
    priority: int
    phase: str | None = None
    system_prompt: str = ""
    context: list = Field(default_factory=list)
    tools: list = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    rationale: str | None = None
    max_tokens: int = 4096
    requirement_ids: list[str] = Field(default_factory=list)


class TaskResultSubmission(BaseModel):
    output_text: str = Field(..., min_length=1, max_length=500_000)
    model_used: str = Field(..., min_length=1, max_length=100)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)


class TaskResultResponse(BaseModel):
    task_id: str
    status: str
    verification_status: str | None = None
    verification_notes: str | None = None
    next_claimable_task_id: str | None = None


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------

class RAGDatabaseInfo(BaseModel):
    name: str
    path: str
    exists: bool
    file_size_bytes: int = 0
    chunk_count: int = 0
    source_count: int = 0
    index_status: str = "unknown"
    sources: list[dict] = Field(default_factory=list)


class RAGChunkPreview(BaseModel):
    id: str
    source: str
    type_name: str | None = None
    file_path: str | None = None
    text_preview: str


# ---------------------------------------------------------------------------
# Wave Reassessment (Athena Loop)
# ---------------------------------------------------------------------------

class TaskOutcomeSummary(BaseModel):
    """Summary of a single task's outcome for reassessment context."""
    task_id: str
    title: str
    status: str
    output_summary: str = ""
    error: str | None = None
    model_tier: str | None = None
    cost_usd: float = 0.0
    duration_seconds: float | None = None


class KnowledgeFinding(BaseModel):
    """A knowledge finding from the project_knowledge table."""
    id: str = ""
    content: str
    category: str = "discovery"
    rationale: str = ""
    alternatives_considered: str = ""
    confidence: str = "medium"
    source_task_title: str = ""


class SentinelObservationSummary(BaseModel):
    """A sentinel observation for wave reassessment context."""
    id: str = ""
    category: str
    severity: str
    message: str
    details: dict = Field(default_factory=dict)


class WaveReassessmentContext(BaseModel):
    """Full context fed to the LLM for wave reassessment."""
    project_id: str
    wave_number: int
    task_outcomes: list[TaskOutcomeSummary]
    knowledge_findings: list[KnowledgeFinding] = Field(default_factory=list)
    sentinel_observations: list[SentinelObservationSummary] = Field(default_factory=list)
    original_plan: dict
    wave_cost_usd: float = 0.0
    all_tasks_terminal: bool = True


class ReassessmentResult(BaseModel):
    """LLM output from the wave reassessment evaluation."""
    outcome: ReassessmentOutcome
    rationale: str = Field(..., min_length=1, max_length=10_000)
    suggested_changes: list[str] = Field(default_factory=list)


class HumanInterventionProposal(BaseModel):
    """Proposal sent when the reassessment outcome is escalate_to_human."""
    project_id: str
    wave_number: int
    rationale: str
    reassessment_context: WaveReassessmentContext
    suggested_actions: list[str] = Field(default_factory=list)
