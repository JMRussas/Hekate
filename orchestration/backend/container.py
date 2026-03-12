#  Orchestration Engine - Dependency Injection Container
#
#  DeclarativeContainer wiring all services and their dependencies.
#  Replaces module-level singletons with injectable providers.
#
#  Depends on: db/connection.py, services/*, tools/registry.py
#  Used by:    app.py, routes/*

import httpx
from dependency_injector import containers, providers

from backend.config import cfg
from backend.db.connection import Database
from backend.services.auth import AuthService
from backend.services.oidc import OIDCService
from backend.services.budget import BudgetManager
from backend.services.decomposer import DecomposerService
from backend.services.executor import Executor
from backend.services.git_service import GitService
from backend.services.planner import PlannerService
from backend.services.progress import ProgressManager
from backend.services.resource_monitor import ResourceMonitor
from backend.services.diagnostic_ingest import DiagnosticIngester
from backend.tools.rag import RAGIndexCache
from backend.tools.registry import ToolRegistry


class Container(containers.DeclarativeContainer):
    """DI container for the Orchestration Engine.

    Most services are Singletons — one instance per application lifecycle.
    Exceptions: git_service, planner, and decomposer use Factory providers
    (new instance per injection) since they hold per-request state.

    Routes access them via @inject + Depends(Provide[Container.xxx]).
    Tests override them via container.xxx.override(providers.Object(mock)).
    """

    wiring_config = containers.WiringConfiguration(
        modules=[
            "backend.routes.projects",
            "backend.routes.tasks",
            "backend.routes.usage",
            "backend.routes.services",
            "backend.routes.events",
            "backend.routes.auth",
            "backend.routes.checkpoints",
            "backend.routes.admin",
            "backend.routes.analytics",
            "backend.routes.rag",
            "backend.routes.auth_oidc",
            "backend.routes.external",
            "backend.routes.internal",
            "backend.middleware.auth",
        ]
    )

    # --- Core ---
    db = providers.Singleton(Database)
    http_client = providers.Singleton(
        httpx.AsyncClient, timeout=cfg("execution.http_client_timeout", 300.0),
    )
    rag_cache = providers.Singleton(RAGIndexCache)
    tool_registry = providers.Singleton(
        ToolRegistry, http_client=http_client, rag_cache=rag_cache,
    )

    # --- Services ---
    auth = providers.Singleton(AuthService, db=db)
    oidc = providers.Singleton(OIDCService, db=db, auth=auth)
    budget = providers.Singleton(BudgetManager, db=db)
    progress = providers.Singleton(ProgressManager, db=db)
    resource_monitor = providers.Singleton(ResourceMonitor)
    diagnostic_ingester = providers.Singleton(DiagnosticIngester)

    # --- Git ---
    git_service = providers.Factory(GitService, db=db)

    # --- Planning & Decomposition ---
    planner = providers.Factory(PlannerService, db=db, budget=budget, tool_registry=tool_registry)
    decomposer = providers.Factory(DecomposerService, db=db)

    # --- Executor (depends on all services) ---
    executor = providers.Singleton(
        Executor,
        db=db,
        budget=budget,
        progress=progress,
        resource_monitor=resource_monitor,
        tool_registry=tool_registry,
        http_client=http_client,
        rag_cache=rag_cache,
        diagnostic_ingester=diagnostic_ingester,
    )
