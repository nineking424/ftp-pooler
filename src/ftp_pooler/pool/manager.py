"""FTP session pool manager."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from ftp_pooler.pool.circuit_breaker import CircuitBreakerOpenError, CircuitBreakerRegistry

import structlog

from ftp_pooler.config.connections import (
    ConnectionRegistry,
    FTPConnectionConfig,
)
from ftp_pooler.config.settings import CircuitBreakerSettings, PoolSettings
from ftp_pooler.pool.circuit_breaker import CircuitBreakerRegistry
from ftp_pooler.pool.session import FTPSession, SessionState


logger = structlog.get_logger(__name__)


class SessionPool:
    """Pool of FTP sessions for a single connection."""

    def __init__(
        self,
        config: FTPConnectionConfig,
        max_sessions: int,
    ) -> None:
        """Initialize the session pool.

        Args:
            config: FTP connection configuration.
            max_sessions: Maximum number of sessions in this pool.
        """
        self.config = config
        self.max_sessions = max_sessions
        self._sessions: list[FTPSession] = []
        self._semaphore = asyncio.Semaphore(max_sessions)
        self._lock = asyncio.Lock()

    @property
    def connection_id(self) -> str:
        """Get the connection ID."""
        return self.config.connection_id

    @property
    def total_sessions(self) -> int:
        """Get total number of sessions."""
        return len(self._sessions)

    @property
    def active_sessions(self) -> int:
        """Get number of active (busy) sessions."""
        return sum(1 for s in self._sessions if s.state == SessionState.BUSY)

    @property
    def available_sessions(self) -> int:
        """Get number of available sessions."""
        return sum(1 for s in self._sessions if s.is_available)

    async def _create_session(self) -> FTPSession:
        """Create a new FTP session.

        Returns:
            New FTP session.

        Raises:
            ConnectionError: If connection fails.
        """
        session_id = f"{self.connection_id}-{uuid.uuid4().hex[:8]}"
        session = FTPSession(
            config=self.config,
            session_id=session_id,
        )
        await session.connect()
        self._sessions.append(session)

        logger.info(
            "session_created",
            session_id=session_id,
            connection_id=self.connection_id,
            total_sessions=self.total_sessions,
        )

        return session

    async def _get_or_create_session(self) -> FTPSession:
        """Get an available session or create a new one.

        Returns:
            Available FTP session.

        Raises:
            RuntimeError: If no session can be obtained.
        """
        async with self._lock:
            # Try to find an available session
            for session in self._sessions:
                if session.is_available:
                    return session

            # Create a new session if under limit
            if self.total_sessions < self.max_sessions:
                return await self._create_session()

            # Try to find a disconnected session to reconnect
            for session in self._sessions:
                if session.state == SessionState.DISCONNECTED:
                    await session.connect()
                    return session

            # No available session found (should not happen with semaphore)
            raise RuntimeError(
                f"No available session for connection: {self.connection_id}"
            )

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FTPSession]:
        """Acquire a session from the pool.

        Yields:
            An FTP session.

        Raises:
            ConnectionError: If session acquisition fails.
        """
        await self._semaphore.acquire()
        try:
            session = await self._get_or_create_session()
            async with session.acquire():
                yield session
        except Exception as e:
            logger.error(
                "session_acquire_error",
                connection_id=self.connection_id,
                error=str(e),
            )
            raise
        finally:
            self._semaphore.release()

    async def close_all(self) -> None:
        """Close all sessions in the pool."""
        async with self._lock:
            for session in self._sessions:
                try:
                    await session.disconnect()
                except Exception as e:
                    logger.warning(
                        "session_close_error",
                        session_id=session.session_id,
                        error=str(e),
                    )
            self._sessions.clear()

        logger.info(
            "pool_closed",
            connection_id=self.connection_id,
        )

    async def health_check(self) -> dict:
        """Perform health check on all sessions.

        Returns:
            Health check results.
        """
        healthy = 0
        unhealthy = 0

        for session in self._sessions:
            if await session.check_health():
                healthy += 1
            else:
                unhealthy += 1

        return {
            "connection_id": self.connection_id,
            "total_sessions": self.total_sessions,
            "healthy": healthy,
            "unhealthy": unhealthy,
            "active": self.active_sessions,
            "available": self.available_sessions,
        }

    def get_stats(self) -> dict:
        """Get pool statistics.

        Returns:
            Pool statistics dictionary.
        """
        return {
            "connection_id": self.connection_id,
            "max_sessions": self.max_sessions,
            "total_sessions": self.total_sessions,
            "active_sessions": self.active_sessions,
            "available_sessions": self.available_sessions,
        }


class SessionPoolManager:
    """Manager for multiple session pools."""

    def __init__(
        self,
        connection_registry: ConnectionRegistry,
        settings: PoolSettings,
        circuit_breaker_settings: Optional[CircuitBreakerSettings] = None,
    ) -> None:
        """Initialize the session pool manager.

        Args:
            connection_registry: Registry of connection configurations.
            settings: Pool settings.
            circuit_breaker_settings: Optional circuit breaker settings.
        """
        self._registry = connection_registry
        self._settings = settings
        self._pools: dict[str, SessionPool] = {}
        self._total_semaphore = asyncio.Semaphore(settings.max_sessions_per_pod)
        self._lock = asyncio.Lock()

        # Initialize circuit breaker registry
        cb_settings = circuit_breaker_settings or CircuitBreakerSettings()
        self._circuit_breakers = CircuitBreakerRegistry(cb_settings)

    @property
    def total_sessions(self) -> int:
        """Get total number of sessions across all pools."""
        return sum(pool.total_sessions for pool in self._pools.values())

    @property
    def active_sessions(self) -> int:
        """Get total number of active sessions across all pools."""
        return sum(pool.active_sessions for pool in self._pools.values())

    async def _get_or_create_pool(self, connection_id: str) -> SessionPool:
        """Get or create a session pool for a connection.

        Args:
            connection_id: The connection ID.

        Returns:
            Session pool for the connection.

        Raises:
            KeyError: If connection is not found.
            TypeError: If connection is not an FTP connection.
        """
        async with self._lock:
            if connection_id not in self._pools:
                config = self._registry.get_ftp(connection_id)
                pool = SessionPool(
                    config=config,
                    max_sessions=self._settings.max_sessions_per_connection,
                )
                self._pools[connection_id] = pool

                logger.info(
                    "pool_created",
                    connection_id=connection_id,
                    max_sessions=self._settings.max_sessions_per_connection,
                )

            return self._pools[connection_id]

    @asynccontextmanager
    async def acquire(self, connection_id: str) -> AsyncIterator[FTPSession]:
        """Acquire a session for a specific connection.

        Args:
            connection_id: The connection ID.

        Yields:
            An FTP session.

        Raises:
            KeyError: If connection is not found.
            ConnectionError: If session acquisition fails.
            CircuitBreakerOpenError: If circuit breaker is open.
        """
        # Check circuit breaker first
        circuit_breaker = self._circuit_breakers.get(connection_id)
        if not circuit_breaker.can_execute():
            logger.warning(
                "circuit_breaker_open",
                connection_id=connection_id,
                state=circuit_breaker.state.value,
                failure_count=circuit_breaker.failure_count,
            )
            raise CircuitBreakerOpenError(
                f"Circuit breaker is open for connection: {connection_id}"
            )

        # Acquire global semaphore first
        await self._total_semaphore.acquire()
        try:
            pool = await self._get_or_create_pool(connection_id)
            async with pool.acquire() as session:
                # Record success on successful acquisition
                circuit_breaker.record_success()
                yield session
        except Exception as e:
            # Record failure on any exception
            circuit_breaker.record_failure()
            logger.warning(
                "circuit_breaker_failure_recorded",
                connection_id=connection_id,
                error=str(e),
                failure_count=circuit_breaker.failure_count,
                state=circuit_breaker.state.value,
            )
            raise
        finally:
            self._total_semaphore.release()

    async def close_all(self) -> None:
        """Close all session pools."""
        async with self._lock:
            for pool in self._pools.values():
                await pool.close_all()
            self._pools.clear()

        logger.info("all_pools_closed")

    async def health_check(self) -> dict:
        """Perform health check on all pools.

        Returns:
            Health check results for all pools.
        """
        results = {}
        for connection_id, pool in self._pools.items():
            results[connection_id] = await pool.health_check()
        return results

    def get_stats(self) -> dict:
        """Get statistics for all pools.

        Returns:
            Statistics dictionary.
        """
        pool_stats = {
            connection_id: pool.get_stats()
            for connection_id, pool in self._pools.items()
        }

        return {
            "max_sessions_per_pod": self._settings.max_sessions_per_pod,
            "max_sessions_per_connection": self._settings.max_sessions_per_connection,
            "total_sessions": self.total_sessions,
            "active_sessions": self.active_sessions,
            "pools": pool_stats,
            "circuit_breakers": self._circuit_breakers.get_all_stats(),
        }
