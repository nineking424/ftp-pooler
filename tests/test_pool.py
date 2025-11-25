"""Tests for session pool module."""

import pytest

from ftp_pooler.config.connections import (
    ConnectionRegistry,
    ConnectionType,
    FTPConnectionConfig,
)
from ftp_pooler.config.settings import PoolSettings
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
