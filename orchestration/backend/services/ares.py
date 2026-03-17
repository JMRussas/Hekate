#  Orchestration Engine - Ares Security Review
#
#  Pre-plan-approval security scanner. Analyzes plan JSON for
#  missing auth, unvalidated DB ops, exposed secrets, plaintext
#  network calls. Critical findings block plan approval.
#
#  Depends on: models/ares.py, models/enums.py, db/connection.py,
#              services/context_store_client.py
#  Used by:    routes/projects.py (plan approval gate)

from __future__ import annotations

import json
import re
import time
import uuid

import httpx
import structlog

from backend.config import cfg
from backend.db.connection import Database
from backend.models.ares import AresReviewResult, SecurityFinding
from backend.models.enums import SecuritySeverity
from backend.services.context_store_client import ContextStoreClient

logger = structlog.get_logger("orchestration.ares")

HEKATE_MCP_URL = cfg("hekate_mcp.url", "http://192.168.1.164:5110")
HEKATE_MCP_DEEP_TIMEOUT = 5.0  # seconds — fast fail if hekate-mcp is down

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_ROUTE_PATH_RE = re.compile(r"(backend|app|src)[/\\]routes?[/\\]", re.IGNORECASE)

_AUTH_KEYWORDS = re.compile(
    r"auth|authentication|authorization|middleware|jwt|bearer|api[_-]?key",
    re.IGNORECASE,
)

_DB_OP_KEYWORDS = re.compile(
    r"\b(sql|query|insert|update|delete|migration|alter\s+table|create\s+table)\b",
    re.IGNORECASE,
)

_VALIDATION_KEYWORDS = re.compile(
    r"validat|sanitiz|parameteriz|pydantic|escape|prepared\s+statement",
    re.IGNORECASE,
)

_SECRET_PATTERNS = re.compile(
    r"\.env|credentials?|secret|api[_-]?key|token|password|\.pem|\.key",
    re.IGNORECASE,
)

_PLAINTEXT_HTTP_RE = re.compile(r"http://", re.IGNORECASE)

_NETWORK_KEYWORDS = re.compile(
    r"external\s+api|network\s+request|webhook|http\s+call|fetch|httpx|requests\.",
    re.IGNORECASE,
)

_TLS_KEYWORDS = re.compile(r"tls|ssl|https", re.IGNORECASE)

_NEW_FILE_KEYWORDS = re.compile(r"\bcreate|new|add\s+file|scaffold\b", re.IGNORECASE)


class AresService:
    """Pre-plan-approval security reviewer (Ares)."""

    def __init__(
        self,
        db: Database,
        context_store_client: ContextStoreClient | None = None,
    ) -> None:
        self.db = db
        self.context_store = context_store_client

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def review_plan(
        self,
        project_id: str,
        plan_id: str,
        plan_json: dict,
        project_row: dict | None = None,
    ) -> AresReviewResult:
        """Scan every task in *plan_json* for security red flags.

        Returns an ``AresReviewResult`` with ``blocked=True`` when at
        least one **critical** finding exists.
        """
        findings: list[SecurityFinding] = []
        all_affected_files: list[str] = []
        task_index = 0

        try:
            phases = plan_json.get("phases", [])
            for phase in phases:
                tasks = phase.get("tasks", [])
                for task in tasks:
                    task_findings = self._analyse_task(task_index, task)
                    findings.extend(task_findings)
                    all_affected_files.extend(task.get("affected_files", []) or [])
                    task_index += 1
        except Exception:
            logger.exception("ares.review_plan crashed — treating as non-blocking")

        # Deep analysis via hekate-mcp (best-effort)
        if all_affected_files and project_row:
            deep_findings = await self._deep_analysis(all_affected_files, project_row)
            findings.extend(deep_findings)

        critical = sum(1 for f in findings if f.severity == SecuritySeverity.CRITICAL)
        warning = sum(1 for f in findings if f.severity == SecuritySeverity.WARNING)
        info = sum(1 for f in findings if f.severity == SecuritySeverity.INFO)

        result = AresReviewResult(
            plan_id=plan_id,
            findings=findings,
            critical_count=critical,
            warning_count=warning,
            info_count=info,
            blocked=critical > 0,
            reviewed_at=time.time(),
        )

        # Persist findings to DB and context store (both fail silently)
        if findings:
            await self._persist_to_db(project_id, plan_id, findings)
            await self._persist_to_context_store(project_id, plan_id, findings)

        logger.info(
            "ares.review_complete",
            project_id=project_id,
            plan_id=plan_id,
            findings=len(findings),
            critical=critical,
            warning=warning,
            blocked=result.blocked,
        )
        return result

    # ------------------------------------------------------------------
    # Private — per-task checks
    # ------------------------------------------------------------------

    def _analyse_task(
        self,
        task_index: int,
        task: dict,
    ) -> list[SecurityFinding]:
        title = task.get("title", "")
        description = task.get("description", "")
        affected_files: list[str] = task.get("affected_files", []) or []
        combined_text = f"{title} {description}"

        findings: list[SecurityFinding] = []

        findings.extend(
            self._check_missing_auth(task_index, title, combined_text, affected_files)
        )
        findings.extend(
            self._check_no_input_validation(task_index, title, combined_text)
        )
        findings.extend(
            self._check_exposed_secrets(task_index, title, affected_files)
        )
        findings.extend(
            self._check_no_tls(task_index, title, combined_text)
        )

        return findings

    # -- Check: missing auth -----------------------------------------------

    def _check_missing_auth(
        self,
        task_index: int,
        title: str,
        combined_text: str,
        affected_files: list[str],
    ) -> list[SecurityFinding]:
        route_files = [f for f in affected_files if _ROUTE_PATH_RE.search(f)]
        mentions_routes = bool(route_files) or bool(_ROUTE_PATH_RE.search(combined_text))

        if not mentions_routes:
            return []

        if _AUTH_KEYWORDS.search(combined_text):
            return []

        # Creating a brand-new route file is critical; touching an existing
        # one without mentioning auth is a warning.
        is_new_route = bool(_NEW_FILE_KEYWORDS.search(combined_text)) and bool(route_files)

        severity = SecuritySeverity.CRITICAL if is_new_route else SecuritySeverity.WARNING
        return [
            SecurityFinding(
                id=str(uuid.uuid4()),
                task_index=task_index,
                task_title=title,
                category="missing_auth",
                severity=severity,
                description=(
                    f"Task touches route file(s) {route_files or ['(inferred from description)']}"
                    " but does not mention authentication or authorization."
                ),
                recommended_mitigation=(
                    "Add auth dependency (e.g. Depends(get_current_user)) to all new "
                    "endpoints, or document why public access is intentional."
                ),
                affected_files=route_files,
            )
        ]

    # -- Check: no input validation ----------------------------------------

    def _check_no_input_validation(
        self,
        task_index: int,
        title: str,
        combined_text: str,
    ) -> list[SecurityFinding]:
        if not _DB_OP_KEYWORDS.search(combined_text):
            return []

        if _VALIDATION_KEYWORDS.search(combined_text):
            return []

        return [
            SecurityFinding(
                id=str(uuid.uuid4()),
                task_index=task_index,
                task_title=title,
                category="no_input_validation",
                severity=SecuritySeverity.WARNING,
                description=(
                    "Task involves database operations but does not mention "
                    "input validation or parameterized queries."
                ),
                recommended_mitigation=(
                    "Use parameterized queries or Pydantic models to validate "
                    "all user-supplied input before database operations."
                ),
            )
        ]

    # -- Check: exposed secrets --------------------------------------------

    def _check_exposed_secrets(
        self,
        task_index: int,
        title: str,
        affected_files: list[str],
    ) -> list[SecurityFinding]:
        flagged = [f for f in affected_files if _SECRET_PATTERNS.search(f)]
        if not flagged:
            return []

        return [
            SecurityFinding(
                id=str(uuid.uuid4()),
                task_index=task_index,
                task_title=title,
                category="exposed_secret",
                severity=SecuritySeverity.CRITICAL,
                description=(
                    f"Affected files contain potential secrets/credentials: {flagged}"
                ),
                recommended_mitigation=(
                    "Move secrets to environment variables or a vault. Never "
                    "commit .env, credential, or key files to the repository."
                ),
                affected_files=flagged,
            )
        ]

    # -- Check: no TLS -----------------------------------------------------

    def _check_no_tls(
        self,
        task_index: int,
        title: str,
        combined_text: str,
    ) -> list[SecurityFinding]:
        has_plaintext = bool(_PLAINTEXT_HTTP_RE.search(combined_text))
        has_network = bool(_NETWORK_KEYWORDS.search(combined_text))

        if not (has_plaintext or has_network):
            return []

        if _TLS_KEYWORDS.search(combined_text):
            return []

        return [
            SecurityFinding(
                id=str(uuid.uuid4()),
                task_index=task_index,
                task_title=title,
                category="no_tls",
                severity=SecuritySeverity.WARNING,
                description=(
                    "Task references network communication or plaintext HTTP "
                    "without mentioning TLS/SSL/HTTPS."
                ),
                recommended_mitigation=(
                    "Use HTTPS for all external communication. Configure "
                    "TLS certificates and reject plaintext HTTP in production."
                ),
            )
        ]

    # ------------------------------------------------------------------
    # Deep analysis via hekate-mcp check_contracts
    # ------------------------------------------------------------------

    async def _deep_analysis(
        self,
        affected_files: list[str],
        project_row: dict,
    ) -> list[SecurityFinding]:
        """Call hekate-mcp check_contracts for deeper static analysis.

        Best-effort: if hekate-mcp is unreachable or errors, returns an
        empty list and logs a warning.  Never blocks the review.
        """
        repo_path = project_row.get("repo_path", "")
        if not repo_path:
            logger.debug("ares.deep_analysis skipped — no repo_path on project")
            return []

        unique_files = sorted(set(affected_files))
        if not unique_files:
            return []

        # Quick reachability check
        try:
            async with httpx.AsyncClient(timeout=HEKATE_MCP_DEEP_TIMEOUT) as client:
                # Ping with an empty JSON-RPC to see if the server is up
                ping = await client.post(
                    f"{HEKATE_MCP_URL}/mcp",
                    json={"jsonrpc": "2.0", "id": 0, "method": "ping", "params": {}},
                )
                # Any HTTP response means the server is up (even a JSON-RPC error)
                if ping.status_code >= 500:
                    logger.warning(
                        "ares.deep_analysis skipped — hekate-mcp returned %s",
                        ping.status_code,
                    )
                    return []
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, OSError):
            logger.warning("ares.deep_analysis skipped — hekate-mcp not reachable at %s", HEKATE_MCP_URL)
            return []

        findings: list[SecurityFinding] = []

        async with httpx.AsyncClient(timeout=HEKATE_MCP_DEEP_TIMEOUT) as client:
            for file_path in unique_files:
                try:
                    payload = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "check_contracts",
                            "arguments": {
                                "file_path": file_path,
                                "project": repo_path,
                            },
                        },
                    }
                    resp = await client.post(f"{HEKATE_MCP_URL}/mcp", json=payload)
                    resp.raise_for_status()
                    data = resp.json()

                    if "error" in data:
                        logger.debug(
                            "ares.deep_analysis check_contracts error for %s: %s",
                            file_path,
                            data["error"],
                        )
                        continue

                    # Parse contract violations from MCP response
                    content = data.get("result", {}).get("content", [])
                    if not content:
                        continue

                    text = content[0].get("text", "") if content[0].get("type") == "text" else ""
                    if not text:
                        continue

                    try:
                        contracts = json.loads(text)
                    except json.JSONDecodeError:
                        # Non-JSON response — skip
                        continue

                    violations = contracts.get("Violations", [])
                    if isinstance(contracts, list):
                        violations = contracts

                    for v in violations:
                        severity = self._map_contract_severity(v)
                        findings.append(
                            SecurityFinding(
                                id=str(uuid.uuid4()),
                                task_index=-1,  # cross-task finding
                                task_title="(deep analysis)",
                                category="contract_violation",
                                severity=severity,
                                description=self._format_violation(file_path, v),
                                recommended_mitigation=self._violation_mitigation(v),
                                affected_files=[file_path],
                            )
                        )
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                    logger.warning(
                        "ares.deep_analysis lost connection to hekate-mcp mid-scan at file %s",
                        file_path,
                    )
                    break  # server went away — stop scanning
                except Exception:
                    logger.debug(
                        "ares.deep_analysis failed for %s",
                        file_path,
                        exc_info=True,
                    )

        if findings:
            logger.info(
                "ares.deep_analysis complete",
                files_scanned=len(unique_files),
                findings=len(findings),
            )
        return findings

    # -- Helpers for deep analysis ------------------------------------------

    @staticmethod
    def _map_contract_severity(violation: dict | str) -> SecuritySeverity:
        """Map a check_contracts violation to a SecuritySeverity."""
        if isinstance(violation, str):
            lowered = violation.lower()
            if any(k in lowered for k in ("error", "missing return", "unchecked")):
                return SecuritySeverity.WARNING
            return SecuritySeverity.INFO

        sev = str(violation.get("Severity", violation.get("severity", ""))).lower()
        if sev in ("error", "critical"):
            return SecuritySeverity.CRITICAL
        if sev in ("warning", "warn"):
            return SecuritySeverity.WARNING
        return SecuritySeverity.INFO

    @staticmethod
    def _format_violation(file_path: str, violation: dict | str) -> str:
        if isinstance(violation, str):
            return f"Contract violation in {file_path}: {violation}"
        rule = violation.get("Rule", violation.get("rule", "unknown"))
        message = violation.get("Message", violation.get("message", str(violation)))
        location = violation.get("Location", violation.get("location", ""))
        loc_str = f" at {location}" if location else ""
        return f"Contract violation in {file_path}{loc_str}: [{rule}] {message}"

    @staticmethod
    def _violation_mitigation(violation: dict | str) -> str:
        if isinstance(violation, dict):
            fix = violation.get("SuggestedFix", violation.get("suggested_fix", ""))
            if fix:
                return fix
        return (
            "Review the flagged code for missing type annotations, "
            "unchecked error returns, or public API contract violations."
        )

    # ------------------------------------------------------------------
    # Persistence — DB
    # ------------------------------------------------------------------

    async def _persist_to_db(
        self,
        project_id: str,
        plan_id: str,
        findings: list[SecurityFinding],
    ) -> None:
        """Insert all findings into the security_findings table.

        Fails silently — logs a warning on error so review results are
        still returned even if persistence breaks.
        """
        try:
            now = time.time()
            statements: list[tuple[str, tuple]] = []
            for f in findings:
                statements.append((
                    "INSERT INTO security_findings "
                    "(id, plan_id, project_id, task_index, task_title, category, "
                    "severity, description, recommended_mitigation, affected_files_json, "
                    "context_store_node_id, created_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
                    (
                        f.id, plan_id, project_id, f.task_index, f.task_title,
                        f.category, f.severity.value, f.description,
                        f.recommended_mitigation, json.dumps(f.affected_files),
                        None, now,
                    ),
                ))
            await self.db.execute_many_write(statements)
            logger.debug(
                "ares.persist_db",
                plan_id=plan_id,
                count=len(findings),
            )
        except Exception:
            logger.warning(
                "ares.persist_db failed — findings not saved to DB",
                plan_id=plan_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Persistence — context store
    # ------------------------------------------------------------------

    async def _persist_to_context_store(
        self,
        project_id: str,
        plan_id: str,
        findings: list[SecurityFinding],
    ) -> None:
        """Create risk nodes in the context store linked to the plan via CONSTRAINS edges.

        Fails silently — matches the circuit breaker pattern. The context store
        is optional infrastructure; findings are still in the DB.
        """
        if not self.context_store:
            return

        try:
            # Look up the plan's context store node via node_mapping
            plan_node_id = await self._resolve_plan_node_id(plan_id)
            if not plan_node_id:
                logger.debug(
                    "ares.persist_context_store skipped — no plan node in context store",
                    plan_id=plan_id,
                )
                return

            for f in findings:
                node_id = await self.context_store.create_node(plan_node_id, {
                    "nodeType": "security_risk",
                    "name": f"{f.severity.value.upper()}: {f.category} (task {f.task_index})",
                    "value": f.description,
                    "attributes": {
                        "severity": f.severity.value,
                        "category": f.category,
                        "description": f.description,
                        "mitigation": f.recommended_mitigation,
                        "affected_files": json.dumps(f.affected_files),
                        "task_index": str(f.task_index),
                        "finding_id": f.id,
                    },
                })

                if not node_id:
                    continue

                # CONSTRAINS edge: risk node → plan node
                await self.context_store.create_edge({
                    "sourceId": node_id,
                    "targetId": plan_node_id,
                    "type": "CONSTRAINS",
                })

                # Update the DB row with the context store node ID
                try:
                    await self.db.execute_write(
                        "UPDATE security_findings SET context_store_node_id = $1 WHERE id = $2",
                        (node_id, f.id),
                    )
                except Exception:
                    logger.debug("ares.update_node_id failed for finding %s", f.id)

            logger.debug(
                "ares.persist_context_store",
                plan_id=plan_id,
                count=len(findings),
            )
        except Exception:
            logger.warning(
                "ares.persist_context_store failed — findings not synced",
                plan_id=plan_id,
                exc_info=True,
            )

    async def _resolve_plan_node_id(self, plan_id: str) -> str | None:
        """Look up the plan's context store node ID from the node_mapping."""
        try:
            row = await self.db.fetchone(
                "SELECT node_mapping_json FROM plans WHERE id = $1", (plan_id,),
            )
            if not row:
                return None
            mapping_raw = row["node_mapping_json"] if isinstance(row, dict) else row[0]
            if not mapping_raw:
                return None
            mapping = json.loads(mapping_raw)
            return mapping.get("__plan_root__")
        except Exception:
            logger.debug("ares.resolve_plan_node_id failed for plan %s", plan_id)
            return None
