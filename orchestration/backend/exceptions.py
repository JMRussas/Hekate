#  Orchestration Engine - Custom Exceptions
#
#  Typed exception hierarchy so routes can map business errors to HTTP
#  status codes without pattern-matching on message strings.
#
#  Depends on: (none)
#  Used by:    services/planner.py, services/decomposer.py, services/git_service.py, routes/projects.py

class OrchestrationError(Exception):
    """Base exception for all orchestration business logic errors."""


class NotFoundError(OrchestrationError):
    """Resource (project, plan, task) does not exist."""


class BudgetExhaustedError(OrchestrationError):
    """Spending limit has been reached."""


class InvalidStateError(OrchestrationError):
    """Operation not allowed in the current resource state."""


class PlanParseError(OrchestrationError):
    """Claude returned a response that couldn't be parsed into a valid plan."""


class CycleDetectedError(OrchestrationError):
    """The task dependency graph contains a cycle."""


class OIDCError(OrchestrationError):
    """Error during OIDC authentication flow."""


class AccountLinkError(OrchestrationError):
    """Error linking or unlinking an OAuth provider."""


class GitError(OrchestrationError):
    """Error performing a git operation."""
