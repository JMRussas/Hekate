#  Orchestration Engine - Prometheus Scrape Endpoint
#
#  Exposes /metrics in Prometheus exposition format.
#  No auth — Prometheus scrapers don't send JWT.
#
#  Depends on: container.py, db/connection.py, services/prometheus_collector.py
#  Used by:    app.py

from dependency_injector.wiring import inject, Provide
from fastapi import APIRouter, Depends
from fastapi.responses import Response
from prometheus_client import generate_latest

from backend.container import Container
from backend.db.connection import Database
from backend.services.prometheus_collector import PrometheusCollector

router = APIRouter(tags=["prometheus"])


@router.get("/metrics")
@inject
async def prometheus_metrics(
    db: Database = Depends(Provide[Container.db]),
) -> Response:
    """Prometheus scrape endpoint — returns metrics in exposition format."""
    collector = PrometheusCollector(db)
    registry = await collector.collect()
    return Response(
        content=generate_latest(registry),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
