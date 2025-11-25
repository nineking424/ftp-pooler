"""API integration tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from ftp_pooler.api.routes import create_app
from ftp_pooler.config.connections import ConnectionRegistry, FTPConnectionConfig, ConnectionType
from ftp_pooler.config.settings import PoolSettings, CircuitBreakerSettings
from ftp_pooler.pool.manager import SessionPoolManager


class MockApplication:
    """Mock Application for testing."""

    def __init__(
        self,
        is_running: bool = True,
        pool_manager: SessionPoolManager = None,
        connection_registry: ConnectionRegistry = None,
    ):
        self._running = is_running
        self._pool_manager = pool_manager
        self._connection_registry = connection_registry

    @property
    def is_running(self) -> bool:
        return self._running

    def get_stats(self) -> dict:
        return {
            "running": self._running,
            "pool": self._pool_manager.get_stats() if self._pool_manager else None,
            "consumer": {
                "tasks_received": 100,
                "parse_errors": 2,
                "total_lag": 50,
                "lag_by_partition": {"ftp-tasks-0": 50},
                "input_topic": "ftp-tasks",
                "consumer_group": "ftp-pooler",
                "running": True,
            },
            "producer": {
                "results_sent": 95,
                "failures_sent": 3,
                "send_errors": 0,
                "pending_count": 0,
                "result_topic": "ftp-results",
                "fail_topic": "ftp-failures",
            },
            "dlq": {
                "dlq_messages_sent": 2,
                "dlq_topic": "ftp-tasks-dlq",
            },
            "transfer": {
                "tasks_in_progress": 5,
                "timeouts": 1,
                "callback_errors": 0,
            },
        }


@pytest.fixture
def mock_connection_registry():
    """Create a mock connection registry."""
    registry = ConnectionRegistry()
    registry.register(
        FTPConnectionConfig(
            connection_id="test-ftp",
            type=ConnectionType.FTP,
            host="localhost",
            port=21,
        )
    )
    return registry


@pytest.fixture
def mock_pool_manager(mock_connection_registry):
    """Create a mock pool manager."""
    manager = SessionPoolManager(
        connection_registry=mock_connection_registry,
        settings=PoolSettings(),
        circuit_breaker_settings=CircuitBreakerSettings(),
    )
    return manager


@pytest.fixture
def mock_app(mock_pool_manager, mock_connection_registry):
    """Create a mock application instance."""
    return MockApplication(
        is_running=True,
        pool_manager=mock_pool_manager,
        connection_registry=mock_connection_registry,
    )


@pytest.fixture
def app(mock_app):
    """Create FastAPI application with mock app instance."""
    return create_app(mock_app)


@pytest.fixture
async def client(app):
    """Create async HTTP client for testing."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


class TestHealthEndpoints:
    """Tests for health check endpoints."""

    @pytest.mark.asyncio
    async def test_health_check(self, client: AsyncClient):
        """Test /health endpoint returns healthy status."""
        response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "0.1.0"

    @pytest.mark.asyncio
    async def test_liveness_check(self, client: AsyncClient):
        """Test /live endpoint returns alive status."""
        response = await client.get("/live")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"

    @pytest.mark.asyncio
    async def test_readiness_check_ready(self, client: AsyncClient):
        """Test /ready endpoint when application is running."""
        response = await client.get("/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"

    @pytest.mark.asyncio
    async def test_readiness_check_not_running(self):
        """Test /ready endpoint when application is not running."""
        mock_app = MockApplication(is_running=False)
        app = create_app(mock_app)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/ready")

            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert data["reason"] == "application_not_running"

    @pytest.mark.asyncio
    async def test_readiness_check_no_app(self):
        """Test /ready endpoint when no application instance."""
        app = create_app(None)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/ready")

            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "not_ready"
            assert data["reason"] == "application_not_initialized"


class TestStatsEndpoints:
    """Tests for statistics endpoints."""

    @pytest.mark.asyncio
    async def test_get_stats(self, client: AsyncClient):
        """Test /stats endpoint returns application statistics."""
        response = await client.get("/stats")

        assert response.status_code == 200
        data = response.json()
        assert data["running"] is True
        assert "pool" in data
        assert "consumer" in data
        assert "producer" in data
        assert "transfer" in data

    @pytest.mark.asyncio
    async def test_get_stats_includes_lag(self, client: AsyncClient):
        """Test /stats endpoint includes consumer lag information."""
        response = await client.get("/stats")

        assert response.status_code == 200
        data = response.json()
        assert "consumer" in data
        assert "total_lag" in data["consumer"]
        assert "lag_by_partition" in data["consumer"]

    @pytest.mark.asyncio
    async def test_get_stats_no_app(self):
        """Test /stats endpoint when no application instance."""
        app = create_app(None)

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/stats")

            assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_get_pool_stats(self, client: AsyncClient):
        """Test /pool/stats endpoint returns pool statistics."""
        response = await client.get("/pool/stats")

        assert response.status_code == 200
        data = response.json()
        assert "max_sessions_per_pod" in data
        assert "max_sessions_per_connection" in data
        assert "total_sessions" in data
        assert "active_sessions" in data
        assert "pools" in data

    @pytest.mark.asyncio
    async def test_get_pool_stats_includes_circuit_breakers(self, client: AsyncClient):
        """Test /pool/stats endpoint includes circuit breaker info."""
        response = await client.get("/pool/stats")

        assert response.status_code == 200
        data = response.json()
        assert "circuit_breakers" in data


class TestPoolEndpoints:
    """Tests for pool management endpoints."""

    @pytest.mark.asyncio
    async def test_get_pool_health(self, client: AsyncClient):
        """Test /pool/health endpoint returns pool health."""
        response = await client.get("/pool/health")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)

    @pytest.mark.asyncio
    async def test_list_connections(self, client: AsyncClient):
        """Test /pool/connections endpoint lists registered connections."""
        response = await client.get("/pool/connections")

        assert response.status_code == 200
        data = response.json()
        assert "connections" in data
        assert isinstance(data["connections"], list)
        assert "test-ftp" in data["connections"]

    @pytest.mark.asyncio
    async def test_get_connection_health_not_found(self, client: AsyncClient):
        """Test /pool/connections/{id}/health for non-existent connection."""
        response = await client.get("/pool/connections/nonexistent/health")

        assert response.status_code == 404
        data = response.json()
        assert "not found" in data["detail"].lower()


class TestCircuitBreakerIntegration:
    """Tests for circuit breaker integration."""

    @pytest.mark.asyncio
    async def test_stats_show_circuit_breaker_state(self, client: AsyncClient):
        """Test that stats endpoint shows circuit breaker states."""
        response = await client.get("/pool/stats")

        assert response.status_code == 200
        data = response.json()
        assert "circuit_breakers" in data


class TestTransferStatsIntegration:
    """Tests for transfer statistics integration."""

    @pytest.mark.asyncio
    async def test_stats_show_callback_errors(self, client: AsyncClient):
        """Test that stats endpoint shows callback errors."""
        response = await client.get("/stats")

        assert response.status_code == 200
        data = response.json()
        assert "transfer" in data
        assert "callback_errors" in data["transfer"]
        assert "timeouts" in data["transfer"]


class TestProducerStatsIntegration:
    """Tests for producer statistics integration."""

    @pytest.mark.asyncio
    async def test_stats_show_producer_pending(self, client: AsyncClient):
        """Test that stats endpoint shows producer pending count."""
        response = await client.get("/stats")

        assert response.status_code == 200
        data = response.json()
        assert "producer" in data
        assert "pending_count" in data["producer"]
        assert "send_errors" in data["producer"]


class TestDLQStatsIntegration:
    """Tests for DLQ statistics integration."""

    @pytest.mark.asyncio
    async def test_stats_show_dlq_messages(self, client: AsyncClient):
        """Test that stats endpoint shows DLQ message count."""
        response = await client.get("/stats")

        assert response.status_code == 200
        data = response.json()
        assert "dlq" in data
        assert "dlq_messages_sent" in data["dlq"]
