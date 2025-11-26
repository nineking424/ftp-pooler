"""Tests for circuit breaker module."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from ftp_pooler.config.settings import CircuitBreakerSettings
from ftp_pooler.pool.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerRegistry,
    CircuitState,
)


class TestCircuitState:
    """Tests for CircuitState enum."""

    def test_circuit_state_values(self) -> None:
        """Test CircuitState enum values."""
        assert CircuitState.CLOSED.value == "closed"
        assert CircuitState.OPEN.value == "open"
        assert CircuitState.HALF_OPEN.value == "half_open"


class TestCircuitBreakerOpenError:
    """Tests for CircuitBreakerOpenError exception."""

    def test_error_message(self) -> None:
        """Test error message formatting."""
        error = CircuitBreakerOpenError("test-connection", 30.5)

        assert error.connection_id == "test-connection"
        assert error.remaining_seconds == 30.5
        assert "test-connection" in str(error)
        assert "30.5" in str(error)


class TestCircuitBreaker:
    """Tests for CircuitBreaker."""

    def test_initial_state(self) -> None:
        """Test circuit breaker initial state."""
        settings = CircuitBreakerSettings(
            failure_threshold=5,
            success_threshold=3,
            timeout_seconds=60,
        )

        breaker = CircuitBreaker("test-conn", settings)

        assert breaker.connection_id == "test-conn"
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0
        assert breaker.success_count == 0

    @pytest.mark.asyncio
    async def test_can_execute_when_closed(self) -> None:
        """Test that closed circuit allows execution."""
        settings = CircuitBreakerSettings(failure_threshold=5)
        breaker = CircuitBreaker("test", settings)

        assert await breaker.can_execute() is True

    @pytest.mark.asyncio
    async def test_opens_after_failure_threshold(self) -> None:
        """Test circuit opens after reaching failure threshold."""
        settings = CircuitBreakerSettings(failure_threshold=3)
        breaker = CircuitBreaker("test", settings)

        # Record failures up to threshold
        await breaker.record_failure()
        assert breaker.state == CircuitState.CLOSED
        await breaker.record_failure()
        assert breaker.state == CircuitState.CLOSED
        await breaker.record_failure()

        # Circuit should be open now
        assert breaker.state == CircuitState.OPEN
        assert breaker.failure_count == 3

    @pytest.mark.asyncio
    async def test_cannot_execute_when_open(self) -> None:
        """Test that open circuit blocks execution."""
        settings = CircuitBreakerSettings(
            failure_threshold=2,
            timeout_seconds=60,
        )
        breaker = CircuitBreaker("test", settings)

        # Open the circuit
        await breaker.record_failure()
        await breaker.record_failure()

        assert breaker.state == CircuitState.OPEN
        assert await breaker.can_execute() is False

    @pytest.mark.asyncio
    async def test_before_call_raises_when_open(self) -> None:
        """Test before_call raises when circuit is open."""
        settings = CircuitBreakerSettings(
            failure_threshold=2,
            timeout_seconds=60,
        )
        breaker = CircuitBreaker("test-conn", settings)

        # Open the circuit
        await breaker.record_failure()
        await breaker.record_failure()

        with pytest.raises(CircuitBreakerOpenError) as exc_info:
            await breaker.before_call()

        assert exc_info.value.connection_id == "test-conn"

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self) -> None:
        """Test that success resets failure count."""
        settings = CircuitBreakerSettings(failure_threshold=5)
        breaker = CircuitBreaker("test", settings)

        # Record some failures
        await breaker.record_failure()
        await breaker.record_failure()
        assert breaker.failure_count == 2

        # Success should reset
        await breaker.record_success()
        assert breaker.failure_count == 0

    @pytest.mark.asyncio
    async def test_half_open_after_timeout(self) -> None:
        """Test circuit transitions to half-open after timeout."""
        settings = CircuitBreakerSettings(
            failure_threshold=2,
            timeout_seconds=0,  # Immediate timeout for testing
        )
        breaker = CircuitBreaker("test", settings)

        # Open the circuit
        await breaker.record_failure()
        await breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        # Wait a moment for timeout
        await asyncio.sleep(0.01)

        # Should transition to half-open on next can_execute
        assert await breaker.can_execute() is True
        assert breaker.state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_half_open_closes_on_success(self) -> None:
        """Test half-open circuit closes after enough successes."""
        settings = CircuitBreakerSettings(
            failure_threshold=2,
            success_threshold=2,
            timeout_seconds=0,
        )
        breaker = CircuitBreaker("test", settings)

        # Open the circuit
        await breaker.record_failure()
        await breaker.record_failure()

        # Wait for timeout
        await asyncio.sleep(0.01)

        # Transition to half-open
        await breaker.can_execute()
        assert breaker.state == CircuitState.HALF_OPEN

        # Record successes
        await breaker.record_success()
        assert breaker.state == CircuitState.HALF_OPEN
        await breaker.record_success()

        # Should be closed now
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_reopens_on_failure(self) -> None:
        """Test half-open circuit reopens on failure."""
        settings = CircuitBreakerSettings(
            failure_threshold=2,
            timeout_seconds=0,
        )
        breaker = CircuitBreaker("test", settings)

        # Open the circuit
        await breaker.record_failure()
        await breaker.record_failure()

        # Wait for timeout
        await asyncio.sleep(0.01)

        # Transition to half-open
        await breaker.can_execute()
        assert breaker.state == CircuitState.HALF_OPEN

        # Single failure should reopen
        await breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

    def test_get_stats(self) -> None:
        """Test get_stats method."""
        settings = CircuitBreakerSettings()
        breaker = CircuitBreaker("test", settings)

        stats = breaker.get_stats()

        assert stats["state"] == "closed"
        assert stats["failure_count"] == 0
        assert stats["success_count"] == 0
        assert stats["last_failure"] is None


class TestCircuitBreakerRegistry:
    """Tests for CircuitBreakerRegistry."""

    @pytest.mark.asyncio
    async def test_get_breaker_creates_new(self) -> None:
        """Test get_breaker creates new circuit breaker."""
        settings = CircuitBreakerSettings()
        registry = CircuitBreakerRegistry(settings)

        breaker = await registry.get_breaker("conn-1")

        assert breaker.connection_id == "conn-1"
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_get_breaker_returns_same(self) -> None:
        """Test get_breaker returns same instance."""
        settings = CircuitBreakerSettings()
        registry = CircuitBreakerRegistry(settings)

        breaker1 = await registry.get_breaker("conn-1")
        breaker2 = await registry.get_breaker("conn-1")

        assert breaker1 is breaker2

    @pytest.mark.asyncio
    async def test_get_all_states(self) -> None:
        """Test get_all_states returns all breaker stats."""
        settings = CircuitBreakerSettings()
        registry = CircuitBreakerRegistry(settings)

        await registry.get_breaker("conn-1")
        await registry.get_breaker("conn-2")

        states = registry.get_all_states()

        assert "conn-1" in states
        assert "conn-2" in states
        assert states["conn-1"]["state"] == "closed"

    @pytest.mark.asyncio
    async def test_get_open_circuits(self) -> None:
        """Test get_open_circuits returns only open circuits."""
        settings = CircuitBreakerSettings(failure_threshold=2)
        registry = CircuitBreakerRegistry(settings)

        breaker1 = await registry.get_breaker("conn-1")
        breaker2 = await registry.get_breaker("conn-2")

        # Open conn-1
        await breaker1.record_failure()
        await breaker1.record_failure()

        open_circuits = registry.get_open_circuits()

        assert "conn-1" in open_circuits
        assert "conn-2" not in open_circuits
