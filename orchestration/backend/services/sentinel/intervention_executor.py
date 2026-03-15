#  Intervention Executor
#
#  Concrete implementations for all sentinel intervention actions.
#  Each method calls the appropriate internal API endpoint or publishes
#  an event, returning a structured InterventionResult.
#
#  Used by: PlanSentinel (after reasoner consultation)

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.services.sentinel.bus import SentinelBus
from backend.services.sentinel.models import SentinelMessage, SentinelObservation

logger = logging.getLogger(__name__)


@dataclass
class InterventionResult:
    """Outcome of an intervention execution attempt."""

    action: str
    success: bool
    detail: str
    task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class InterventionExecutor:
    """Executes concrete intervention actions against the orchestration API.

    Handles: retry_task, release_claim, skip_task, reorder_wave.
    Uses a shared httpx.AsyncClient with lazy initialisation.
    """

    def __init__(
        self,
        base_url: str,
        bus: SentinelBus,
        auth_token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bus = bus
        self._auth_token = auth_token
        self._timeout = timeout
        self._http_client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
            )
        return self._http_client

    def _headers(self) -> dict[str, str]:
        if self._auth_token:
            return {"Authorization": f"Bearer {self._auth_token}"}
        return {}

    async def close(self) -> None:
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    # ------------------------------------------------------------------
    # Intervention actions
    # ------------------------------------------------------------------

    async def retry_task(self, obs: SentinelObservation) -> InterventionResult:
        """Re-queue a failed task via POST /api/tasks/{id}/retry."""
        task_id = _extract_task_id(obs)
        if not task_id:
            return InterventionResult(
                action="retry_task",
                success=False,
                detail="no task_id available on observation",
            )

        url = f"{self._base_url}/api/tasks/{task_id}/retry"
        try:
            resp = await self._get_client().post(url, headers=self._headers())
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning("retry_task request failed for %s: %s", task_id[:8], exc)
            return InterventionResult(
                action="retry_task",
                success=False,
                detail=f"request error: {type(exc).__name__}",
                task_id=task_id,
            )

        if resp.status_code == 200:
            logger.info("Intervention executor retried task %s", task_id[:8])
            return InterventionResult(
                action="retry_task",
                success=True,
                detail="task re-queued",
                task_id=task_id,
                metadata=_safe_json(resp),
            )

        detail = _resp_detail(resp)
        logger.warning("retry_task failed for %s: %s", task_id[:8], detail)
        return InterventionResult(
            action="retry_task",
            success=False,
            detail=detail,
            task_id=task_id,
        )

    async def release_claim(self, obs: SentinelObservation) -> InterventionResult:
        """Release a stuck task's claim via POST /api/external/tasks/{id}/release."""
        task_id = _extract_task_id(obs)
        if not task_id:
            return InterventionResult(
                action="release_claim",
                success=False,
                detail="no task_id available on observation",
            )

        url = f"{self._base_url}/api/external/tasks/{task_id}/release"
        try:
            resp = await self._get_client().post(url, headers=self._headers())
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning("release_claim request failed for %s: %s", task_id[:8], exc)
            return InterventionResult(
                action="release_claim",
                success=False,
                detail=f"request error: {type(exc).__name__}",
                task_id=task_id,
            )

        if resp.status_code == 200:
            logger.info("Intervention executor released claim on task %s", task_id[:8])
            return InterventionResult(
                action="release_claim",
                success=True,
                detail="claim released",
                task_id=task_id,
                metadata=_safe_json(resp),
            )

        detail = _resp_detail(resp)
        logger.warning("release_claim failed for %s: %s", task_id[:8], detail)
        return InterventionResult(
            action="release_claim",
            success=False,
            detail=detail,
            task_id=task_id,
        )

    async def skip_task(self, obs: SentinelObservation) -> InterventionResult:
        """Cancel a task and unblock dependents via PATCH /api/tasks/{id}.

        Sets status to ``cancelled`` so the wave dispatch logic can
        proceed past the blocking task.  Dependent tasks are unblocked
        by publishing a ``stall_notification`` event that downstream
        consumers (executor) can act on.
        """
        task_id = _extract_task_id(obs)
        if not task_id:
            return InterventionResult(
                action="skip_task",
                success=False,
                detail="no task_id available on observation",
            )

        url = f"{self._base_url}/api/tasks/{task_id}"
        payload = {"status": "cancelled"}
        try:
            resp = await self._get_client().patch(
                url, json=payload, headers=self._headers(),
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning("skip_task request failed for %s: %s", task_id[:8], exc)
            return InterventionResult(
                action="skip_task",
                success=False,
                detail=f"request error: {type(exc).__name__}",
                task_id=task_id,
            )

        if resp.status_code == 200:
            # Notify downstream that dependents should be unblocked
            await self._bus.publish(SentinelMessage(
                topic="stall_notification",
                source="intervention_executor",
                payload={
                    "type": "task_skipped",
                    "task_id": task_id,
                    "observation_id": obs.observation_id,
                    "project_id": obs.project_id,
                },
            ))
            logger.info("Intervention executor skipped task %s", task_id[:8])
            return InterventionResult(
                action="skip_task",
                success=True,
                detail="task cancelled, dependents notified",
                task_id=task_id,
                metadata=_safe_json(resp),
            )

        detail = _resp_detail(resp)
        logger.warning("skip_task failed for %s: %s", task_id[:8], detail)
        return InterventionResult(
            action="skip_task",
            success=False,
            detail=detail,
            task_id=task_id,
        )

    async def reorder_wave(self, obs: SentinelObservation) -> InterventionResult:
        """Publish a wave-reorder proposal event (no direct mutation).

        Wave reordering is always supervised — the executor publishes a
        proposal via the SentinelBus and returns success.  Actual
        reordering happens only after user approval.
        """
        wave = obs.details.get("wave")
        stalled_ids = obs.details.get("stalled_task_ids", [])
        project_id = obs.project_id

        if wave is None:
            return InterventionResult(
                action="reorder_wave",
                success=False,
                detail="no wave number in observation details",
            )

        await self._bus.publish(SentinelMessage(
            topic="intervention_proposal",
            source="intervention_executor",
            payload={
                "type": "reorder_wave_proposal",
                "action": "reorder_wave",
                "project_id": project_id,
                "wave": wave,
                "stalled_task_ids": stalled_ids,
                "observation_id": obs.observation_id,
                "category": obs.category,
                "severity": obs.severity.value,
                "recommendation": (
                    f"Reorder wave {wave} — {len(stalled_ids)} task(s) stalled. "
                    "Consider reprioritising or redistributing work."
                ),
            },
        ))

        logger.info(
            "Intervention executor proposed wave %s reorder for project %s",
            wave,
            (project_id or "?")[:8],
        )
        return InterventionResult(
            action="reorder_wave",
            success=True,
            detail=f"reorder proposal published for wave {wave}",
            metadata={"wave": wave, "stalled_task_ids": stalled_ids},
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _extract_task_id(obs: SentinelObservation) -> str | None:
    """Pull a task ID from the observation, falling back to details."""
    if obs.task_id:
        return obs.task_id
    # cascade_failure stores a list of failed IDs — take the last one
    failed_ids = obs.details.get("failed_task_ids", [])
    return failed_ids[-1] if failed_ids else None


def _resp_detail(resp: httpx.Response) -> str:
    """Build a concise error detail from an HTTP response."""
    body = resp.text[:200] if resp.text else ""
    return f"API returned {resp.status_code}: {body}"


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    """Try to parse response JSON, return empty dict on failure."""
    try:
        return resp.json()
    except Exception:
        return {}
