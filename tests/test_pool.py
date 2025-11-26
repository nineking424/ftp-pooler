"""Tests for session pool module."""

import pytest

from ftp_pooler.config.connections import (
    ConnectionRegistry,
    ConnectionType,
    FTPConnectionConfig,
)
from ftp_pooler.config.settings import CircuitBreakerSettings, PoolSettings
from ftp_pooler.pool.manager import SessionPool, SessionPoolManager
from ftp_pooler.pool.session import FTPSession, SessionState


class TestFTPSession:
    """Tests for FTPSession."""

    def test_session_initial_state(self) -> None:
        """Test initial session state."""
        config = FTPConnectionConfig(
            connection_id="test",
            type=ConnectionType.FTP,
            host="ftp.example.com",
        )

        session = FTPSession(
            config=config,
            session_id="test-session-1",
        )

        assert session.state == SessionState.DISCONNECTED
        assert session.is_connected is False
        assert session.is_available is False
        assert session.error_count == 0

    def test_session_to_dict(self) -> None:
        """Test session to_dict method."""
        config = FTPConnectionConfig(
            connection_id="test",
            type=ConnectionType.FTP,
            host="ftp.example.com",
        )

        session = FTPSession(
            config=config,
            session_id="test-session-1",
        )

        data = session.to_dict()

        assert data["session_id"] == "test-session-1"
        assert data["connection_id"] == "test"
        assert data["host"] == "ftp.example.com"
        assert data["state"] == "disconnected"
        assert data["error_count"] == 0


class TestSessionPool:
    """Tests for SessionPool."""

    def test_pool_initial_state(self) -> None:
        """Test initial pool state."""
        config = FTPConnectionConfig(
            connection_id="test",
            type=ConnectionType.FTP,
            host="ftp.example.com",
        )

        pool = SessionPool(config=config, max_sessions=10)

        assert pool.connection_id == "test"
        assert pool.max_sessions == 10
        assert pool.total_sessions == 0
        assert pool.active_sessions == 0
        assert pool.available_sessions == 0

    def test_pool_get_stats(self) -> None:
        """Test pool statistics."""
        config = FTPConnectionConfig(
            connection_id="test",
            type=ConnectionType.FTP,
            host="ftp.example.com",
        )

        pool = SessionPool(config=config, max_sessions=10)
        stats = pool.get_stats()

        assert stats["connection_id"] == "test"
        assert stats["max_sessions"] == 10
        assert stats["total_sessions"] == 0


class TestSessionPoolManager:
    """Tests for SessionPoolManager."""

    def test_manager_initial_state(self) -> None:
        """Test initial manager state."""
        registry = ConnectionRegistry()
        registry.register(
            FTPConnectionConfig(
                connection_id="test",
                type=ConnectionType.FTP,
                host="ftp.example.com",
            )
        )

        settings = PoolSettings(
            max_sessions_per_pod=100,
            max_sessions_per_connection=10,
        )

        manager = SessionPoolManager(
            connection_registry=registry,
            settings=settings,
        )

        assert manager.total_sessions == 0
        assert manager.active_sessions == 0

    def test_manager_get_stats(self) -> None:
        """Test manager statistics."""
        registry = ConnectionRegistry()
        registry.register(
            FTPConnectionConfig(
                connection_id="test",
                type=ConnectionType.FTP,
                host="ftp.example.com",
            )
        )

        settings = PoolSettings(
            max_sessions_per_pod=100,
            max_sessions_per_connection=10,
        )

        manager = SessionPoolManager(
            connection_registry=registry,
            settings=settings,
        )

        stats = manager.get_stats()

        assert stats["max_sessions_per_pod"] == 100
        assert stats["max_sessions_per_connection"] == 10
        assert stats["total_sessions"] == 0
        assert stats["pools"] == {}


class TestSessionState:
    """Tests for SessionState enum."""

    def test_session_state_values(self) -> None:
        """Test SessionState enum values."""
        assert SessionState.IDLE.value == "idle"
        assert SessionState.BUSY.value == "busy"
        assert SessionState.DISCONNECTED.value == "disconnected"
        assert SessionState.ERROR.value == "error"


class TestPoolSettings:
    """Tests for PoolSettings with pre-warming."""

    def test_default_prewarm_settings(self) -> None:
        """Test default pre-warm settings."""
        settings = PoolSettings()

        assert settings.prewarm_enabled is True
        assert settings.prewarm_sessions_per_connection == 2
        assert settings.prewarm_timeout_seconds == 30

    def test_custom_prewarm_settings(self) -> None:
        """Test custom pre-warm settings."""
        settings = PoolSettings(
            prewarm_enabled=False,
            prewarm_sessions_per_connection=5,
            prewarm_timeout_seconds=60,
        )

        assert settings.prewarm_enabled is False
        assert settings.prewarm_sessions_per_connection == 5
        assert settings.prewarm_timeout_seconds == 60


class TestSessionPoolManagerWithCircuitBreaker:
    """Tests for SessionPoolManager with circuit breaker."""

    def test_manager_with_circuit_breaker_settings(self) -> None:
        """Test manager initialization with circuit breaker settings."""
        registry = ConnectionRegistry()
        registry.register(
            FTPConnectionConfig(
                connection_id="test",
                type=ConnectionType.FTP,
                host="ftp.example.com",
            )
        )

        pool_settings = PoolSettings()
        cb_settings = CircuitBreakerSettings(
            failure_threshold=5,
            success_threshold=3,
            timeout_seconds=60,
        )

        manager = SessionPoolManager(
            connection_registry=registry,
            settings=pool_settings,
            circuit_breaker_settings=cb_settings,
        )

        stats = manager.get_stats()

        assert "circuit_breakers" in stats
        assert stats["max_sessions_per_pod"] == 100

    def test_manager_stats_include_circuit_breakers(self) -> None:
        """Test manager stats include circuit breaker info."""
        registry = ConnectionRegistry()
        registry.register(
            FTPConnectionConfig(
                connection_id="test-ftp",
                type=ConnectionType.FTP,
                host="ftp.example.com",
            )
        )

        settings = PoolSettings()
        manager = SessionPoolManager(
            connection_registry=registry,
            settings=settings,
        )

        stats = manager.get_stats()

        assert "circuit_breakers" in stats
        assert isinstance(stats["circuit_breakers"], dict)
