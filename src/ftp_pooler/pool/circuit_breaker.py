"""Circuit breaker pattern implementation for FTP connections."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

import structlog

from ftp_pooler.config.settings import CircuitBreakerSettings


logger = structlog.get_logger(__name__)


class CircuitState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Blocking all calls
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open."""

    def __init__(self, connection_id: str, remaining_seconds: float):
        self.connection_id = connection_id
        self.remaining_seconds = remaining_seconds
        super().__init__(
            f"Circuit breaker is open for connection '{connection_id}'. "
            f"Retry in {remaining_seconds:.1f}s"
        )


class CircuitBreaker:
    """Circuit breaker for a single connection."""

    def __init__(
        self,
        connection_id: str,
        settings: CircuitBreakerSettings,
    ) -> None:
        """Initialize the circuit breaker.

        Args:
            connection_id: ID of the connection.
            settings: Circuit breaker settings.
        """
        self._connection_id = connection_id
        self._settings = settings
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[datetime] = None
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    @property
    def connection_id(self) -> str:
        """Get the connection ID."""
        return self._connection_id

    @property
    def state(self) -> CircuitState:
        """Get the current state."""
        return self._state

    @property
    def failure_count(self) -> int:
        """Get the failure count."""
        return self._failure_count

    @property
    def success_count(self) -> int:
        """Get the success count (in half-open state)."""
        return self._success_count

    def _should_attempt_reset(self) -> bool:
        """Check if we should attempt to reset the circuit.

        Returns:
            True if timeout has passed since last failure.
        """
        if self._last_failure_time is None:
            return True

        timeout = timedelta(seconds=self._settings.timeout_seconds)
        return datetime.now(timezone.utc) - self._last_failure_time > timeout

    def _get_remaining_timeout(self) -> float:
        """Get remaining timeout in seconds.

        Returns:
            Seconds until circuit can be tested.
        """
        if self._last_failure_time is None:
            return 0.0

        timeout = timedelta(seconds=self._settings.timeout_seconds)
        elapsed = datetime.now(timezone.utc) - self._last_failure_time
        remaining = (timeout - elapsed).total_seconds()
        return max(0.0, remaining)

    async def can_execute(self) -> bool:
        """Check if a call can be executed.

        Returns:
            True if the circuit allows execution.
        """
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                if self._should_attempt_reset():
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    self._success_count = 0
                    logger.info(
                        "circuit_breaker_half_open",
                        connection_id=self._connection_id,
                    )
                    return True
                return False

            # HALF_OPEN state
            return self._half_open_calls < self._settings.half_open_max_calls

    async def record_success(self) -> None:
        """Record a successful call."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self._settings.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
                    logger.info(
                        "circuit_breaker_closed",
                        connection_id=self._connection_id,
                    )
            else:
                # Reset failure count on success in closed state
                self._failure_count = 0

    async def record_failure(self) -> None:
        """Record a failed call."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = datetime.now(timezone.utc)

            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open immediately opens the circuit
                self._state = CircuitState.OPEN
                logger.warning(
                    "circuit_breaker_reopened",
                    connection_id=self._connection_id,
                )
            elif self._failure_count >= self._settings.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    "circuit_breaker_opened",
                    connection_id=self._connection_id,
                    failures=self._failure_count,
                    threshold=self._settings.failure_threshold,
                )

    async def before_call(self) -> None:
        """Call before executing an operation.

        Raises:
            CircuitBreakerOpenError: If the circuit is open.
        """
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_calls += 1

        if not await self.can_execute():
            raise CircuitBreakerOpenError(
                self._connection_id,
                self._get_remaining_timeout(),
            )

    def get_stats(self) -> dict:
        """Get circuit breaker statistics.

        Returns:
            Statistics dictionary.
        """
        return {
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure": self._last_failure_time.isoformat() if self._last_failure_time else None,
        }


class CircuitBreakerRegistry:
    """Registry of circuit breakers for all connections."""

    def __init__(self, settings: CircuitBreakerSettings) -> None:
        """Initialize the registry.

        Args:
            settings: Circuit breaker settings.
        """
        self._settings = settings
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = asyncio.Lock()

    async def get_breaker(self, connection_id: str) -> CircuitBreaker:
        """Get or create a circuit breaker for a connection.

        Args:
            connection_id: ID of the connection.

        Returns:
            CircuitBreaker instance.
        """
        async with self._lock:
            if connection_id not in self._breakers:
                self._breakers[connection_id] = CircuitBreaker(
                    connection_id, self._settings
                )
            return self._breakers[connection_id]

    def get_all_states(self) -> dict[str, dict]:
        """Get states of all circuit breakers.

        Returns:
            Dictionary mapping connection ID to stats.
        """
        return {
            conn_id: breaker.get_stats()
            for conn_id, breaker in self._breakers.items()
        }

    def get_open_circuits(self) -> list[str]:
        """Get list of connections with open circuits.

        Returns:
            List of connection IDs with open circuits.
        """
        return [
            conn_id
            for conn_id, breaker in self._breakers.items()
            if breaker.state == CircuitState.OPEN
        ]
