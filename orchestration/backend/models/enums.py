#  Orchestration Engine - Enums
#
#  Status and type enumerations used across the system.
#
#  Depends on: (none)
#  Used by:    models/schemas.py, services/*, routes/*

from enum import Enum


class ProjectStatus(str, Enum):
    DRAFT = "draft"
    PLANNING = "planning"
    READY = "ready"          # Plan approved, awaiting execution
    EXECUTING = "executing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class TaskStatus(str, Enum):
    PENDING = "pending"
    BLOCKED = "blocked"      # Dependencies not met
    QUEUED = "queued"        # Ready, waiting for worker
    RUNNING = "running"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"  # Output requires human review
    FAILED = "failed"
    CANCELLED = "cancelled"


class VerificationResult(str, Enum):
    PASSED = "passed"
    GAPS_FOUND = "gaps_found"
    HUMAN_NEEDED = "human_needed"
    SKIPPED = "skipped"


class ModelTier(str, Enum):
    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"
    OLLAMA = "ollama"


class TaskType(str, Enum):
    CODE = "code"
    RESEARCH = "research"
    ANALYSIS = "analysis"
    ASSET = "asset"
    INTEGRATION = "integration"
    DOCUMENTATION = "documentation"


class TaskSortField(str, Enum):
    PRIORITY = "priority"
    CREATED_AT = "created_at"
    WAVE = "wave"
    STATUS = "status"


class PlanningRigor(str, Enum):
    L1 = "L1"   # Quick — flat task list
    L2 = "L2"   # Standard — phases + open questions
    L3 = "L3"   # Thorough — phases + risk + test strategy


class ExecutionMode(str, Enum):
    AUTO = "auto"           # Engine executes all tasks (current behavior)
    HYBRID = "hybrid"       # Engine executes Ollama, external handles Claude tiers
    EXTERNAL = "external"   # External handles everything


class FindingCategory(str, Enum):
    CONSTRAINT = "constraint"       # "X must be Y", "never do Z"
    DECISION = "decision"           # "we chose X over Y because..."
    DISCOVERY = "discovery"         # API behavior, library quirks
    REFERENCE = "reference"         # useful URLs, doc pointers, code patterns
    GOTCHA = "gotcha"               # things that don't work as expected
    ARCHITECTURE = "architecture"   # structural choices, data flow


class ResourceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    DEGRADED = "degraded"    # Reachable but missing models
    CHECKING = "checking"    # Initial state before first health check
