"""REST API routes using FastAPI."""

from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import structlog


logger = structlog.get_logger(__name__)


# Request/Response models
class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    version: str


class StatsResponse(BaseModel):
    """Application statistics response."""

    running: bool
    pool: Optional[dict] = None
    consumer: Optional[dict] = None
    producer: Optional[dict] = None
    transfer: Optional[dict] = None


class PoolStatsResponse(BaseModel):
    """Pool statistics response."""

    max_sessions_per_pod: int
    max_sessions_per_connection: int
    total_sessions: int
    active_sessions: int
    pools: dict


class ConnectionHealthResponse(BaseModel):
    """Connection health check response."""

    connection_id: str
    total_sessions: int
    healthy: int
    unhealthy: int
    active: int
    available: int


# Global reference to application (set by create_app)
_app_instance = None


def create_app(app_instance=None) -> FastAPI:
    """Create FastAPI application.

    Args:
        app_instance: Optional Application instance for dependency injection.

    Returns:
        Configured FastAPI application.
    """
    global _app_instance
    _app_instance = app_instance

    api = FastAPI(
        title="FTP Pooler",
        description="High-performance distributed FTP file transfer system",
        version="0.1.0",
    )

    @api.get("/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:
        """Health check endpoint.

        Returns:
            Health status and version.
        """
        return HealthResponse(
            status="healthy",
            version="0.1.0",
        )

    @api.get("/ready")
    async def readiness_check() -> JSONResponse:
        """Kubernetes readiness probe endpoint.

        Returns:
            Ready status.
        """
        if _app_instance is None:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": "application_not_initialized"},
            )

        if not _app_instance.is_running:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "reason": "application_not_running"},
            )

        return JSONResponse(
            status_code=200,
            content={"status": "ready"},
        )

    @api.get("/live")
    async def liveness_check() -> JSONResponse:
        """Kubernetes liveness probe endpoint.

        Returns:
            Live status.
        """
        return JSONResponse(
            status_code=200,
            content={"status": "alive"},
        )

    @api.get("/stats", response_model=StatsResponse)
    async def get_stats() -> StatsResponse:
        """Get application statistics.

        Returns:
            Application statistics.

        Raises:
            HTTPException: If application is not initialized.
        """
        if _app_instance is None:
            raise HTTPException(
                status_code=503,
                detail="Application not initialized",
            )

        stats = _app_instance.get_stats()
        return StatsResponse(**stats)

    @api.get("/pool/stats", response_model=PoolStatsResponse)
    async def get_pool_stats() -> PoolStatsResponse:
        """Get session pool statistics.

        Returns:
            Pool statistics.

        Raises:
            HTTPException: If pool manager is not available.
        """
        if _app_instance is None or _app_instance._pool_manager is None:
            raise HTTPException(
                status_code=503,
                detail="Pool manager not available",
            )

        stats = _app_instance._pool_manager.get_stats()
        return PoolStatsResponse(**stats)

    @api.get("/pool/health")
    async def get_pool_health() -> JSONResponse:
        """Get session pool health.

        Returns:
            Pool health status for all connections.

        Raises:
            HTTPException: If pool manager is not available.
        """
        if _app_instance is None or _app_instance._pool_manager is None:
            raise HTTPException(
                status_code=503,
                detail="Pool manager not available",
            )

        health = await _app_instance._pool_manager.health_check()
        return JSONResponse(content=health)

    @api.get("/pool/connections")
    async def list_connections() -> JSONResponse:
        """List all registered connections.

        Returns:
            List of connection IDs.

        Raises:
            HTTPException: If connection registry is not available.
        """
        if _app_instance is None or _app_instance._connection_registry is None:
            raise HTTPException(
                status_code=503,
                detail="Connection registry not available",
            )

        connections = _app_instance._connection_registry.list_connections()
        return JSONResponse(content={"connections": connections})

    @api.get("/pool/connections/{connection_id}/health")
    async def get_connection_health(connection_id: str) -> ConnectionHealthResponse:
        """Get health status for a specific connection.

        Args:
            connection_id: The connection ID.

        Returns:
            Connection health status.

        Raises:
            HTTPException: If connection is not found.
        """
        if _app_instance is None or _app_instance._pool_manager is None:
            raise HTTPException(
                status_code=503,
                detail="Pool manager not available",
            )

        pool = _app_instance._pool_manager._pools.get(connection_id)
        if pool is None:
            raise HTTPException(
                status_code=404,
                detail=f"Connection not found: {connection_id}",
            )

        health = await pool.health_check()
        return ConnectionHealthResponse(**health)

    @api.on_event("startup")
    async def startup_event() -> None:
        """Handle application startup."""
        logger.info("api_started")

    @api.on_event("shutdown")
    async def shutdown_event() -> None:
        """Handle application shutdown."""
        logger.info("api_shutdown")

    return api
