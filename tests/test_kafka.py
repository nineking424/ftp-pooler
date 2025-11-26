"""Tests for Kafka module (consumer, producer, DLQ)."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ftp_pooler.config.settings import KafkaSettings
from ftp_pooler.kafka.consumer import TaskConsumer, _calculate_backoff, _safe_json_deserialize
from ftp_pooler.kafka.dlq import DLQMessage, DLQProducer, _calculate_backoff as dlq_calculate_backoff
from ftp_pooler.kafka.producer import ResultProducer
from ftp_pooler.transfer.models import TransferResult, TransferStatus, TransferTask


class TestBackoffCalculation:
    """Tests for exponential backoff calculation."""

    def test_first_retry_uses_base(self) -> None:
        """Test first retry uses base backoff."""
        backoff = _calculate_backoff(
            retry_count=1,
            base_backoff_ms=1000,
            max_backoff_ms=30000,
        )

        # Should be around 1 second (±20% jitter)
        assert 0.8 <= backoff <= 1.2

    def test_exponential_growth(self) -> None:
        """Test backoff grows exponentially."""
        backoff_1 = _calculate_backoff(1, 1000, 30000)
        backoff_2 = _calculate_backoff(2, 1000, 30000)
        backoff_3 = _calculate_backoff(3, 1000, 30000)

        # Roughly doubling each time (with jitter)
        assert backoff_2 > backoff_1 * 1.5
        assert backoff_3 > backoff_2 * 1.5

    def test_capped_at_max(self) -> None:
        """Test backoff is capped at max."""
        backoff = _calculate_backoff(
            retry_count=10,  # Would be 1000 * 2^9 = 512000 without cap
            base_backoff_ms=1000,
            max_backoff_ms=30000,
        )

        # Should be around 30 seconds (±20% jitter)
        assert backoff <= 36.0  # 30 * 1.2


class TestSafeJsonDeserialize:
    """Tests for safe JSON deserialization."""

    def test_deserialize_valid_json(self) -> None:
        """Test deserializing valid JSON."""
        data = b'{"key": "value", "number": 42}'
        result = _safe_json_deserialize(data)

        assert result == {"key": "value", "number": 42}

    def test_deserialize_empty_returns_none(self) -> None:
        """Test empty data returns None."""
        result = _safe_json_deserialize(b"")
        assert result is None

        result = _safe_json_deserialize(None)
        assert result is None

    def test_deserialize_invalid_json_returns_none(self) -> None:
        """Test invalid JSON returns None."""
        result = _safe_json_deserialize(b"not json")
        assert result is None

    def test_deserialize_invalid_utf8_returns_none(self) -> None:
        """Test invalid UTF-8 returns None."""
        result = _safe_json_deserialize(b"\xff\xfe")
        assert result is None


class TestDLQMessage:
    """Tests for DLQMessage."""

    def test_create_dlq_message(self) -> None:
        """Test creating DLQ message."""
        msg = DLQMessage(
            original_topic="ftp-tasks",
            original_partition=0,
            original_offset=123,
            original_key=b"key123",
            original_value=b'{"task_id": "test"}',
            error_type="PARSE_ERROR",
            error_message="Invalid JSON",
        )

        assert msg.original_topic == "ftp-tasks"
        assert msg.original_partition == 0
        assert msg.original_offset == 123
        assert msg.error_type == "PARSE_ERROR"
        assert msg.timestamp is not None

    def test_to_dict(self) -> None:
        """Test DLQ message to_dict method."""
        msg = DLQMessage(
            original_topic="ftp-tasks",
            original_partition=1,
            original_offset=456,
            original_key=b"key456",
            original_value=b'{"data": "value"}',
            error_type="VALIDATION_ERROR",
            error_message="Missing required field",
            timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
        )

        data = msg.to_dict()

        assert data["original_topic"] == "ftp-tasks"
        assert data["original_partition"] == 1
        assert data["original_offset"] == 456
        assert data["original_key"] == "key456"
        assert data["original_value"] == '{"data": "value"}'
        assert data["error_type"] == "VALIDATION_ERROR"
        assert data["error_message"] == "Missing required field"
        assert "2025-01-15" in data["timestamp"]

    def test_to_dict_with_binary_value(self) -> None:
        """Test to_dict with non-UTF8 binary value."""
        msg = DLQMessage(
            original_topic="ftp-tasks",
            original_partition=0,
            original_offset=0,
            original_key=None,
            original_value=b"\xff\xfe\xfd",
            error_type="PARSE_ERROR",
            error_message="Binary data",
        )

        data = msg.to_dict()

        # Should be base64 encoded
        assert data["original_value"] is not None
        assert data["original_key"] is None


class TestDLQProducer:
    """Tests for DLQProducer."""

    def test_initial_state(self) -> None:
        """Test DLQ producer initial state."""
        settings = KafkaSettings(dlq_topic="test-dlq")
        producer = DLQProducer(settings)

        assert producer.messages_sent == 0

    def test_get_stats(self) -> None:
        """Test DLQ producer statistics."""
        settings = KafkaSettings(dlq_topic="test-dlq")
        producer = DLQProducer(settings)

        stats = producer.get_stats()

        assert stats["dlq_messages_sent"] == 0
        assert stats["dlq_topic"] == "test-dlq"


class TestTaskConsumer:
    """Tests for TaskConsumer."""

    def test_initial_state(self) -> None:
        """Test consumer initial state."""
        settings = KafkaSettings(
            input_topic="ftp-tasks",
            consumer_group="test-group",
        )
        consumer = TaskConsumer(settings)

        assert consumer.is_running is False
        assert consumer.tasks_received == 0
        assert consumer.parse_errors == 0
        assert consumer.total_lag == 0

    def test_lag_by_partition(self) -> None:
        """Test lag by partition accessor."""
        settings = KafkaSettings()
        consumer = TaskConsumer(settings)

        # Set internal lag data
        consumer._lag = {
            ("ftp-tasks", 0): 10,
            ("ftp-tasks", 1): 20,
        }

        lag_by_partition = consumer.lag_by_partition

        assert lag_by_partition["ftp-tasks-0"] == 10
        assert lag_by_partition["ftp-tasks-1"] == 20

    def test_total_lag(self) -> None:
        """Test total lag calculation."""
        settings = KafkaSettings()
        consumer = TaskConsumer(settings)

        consumer._lag = {
            ("ftp-tasks", 0): 10,
            ("ftp-tasks", 1): 20,
            ("ftp-tasks", 2): 5,
        }

        assert consumer.total_lag == 35

    def test_track_offset(self) -> None:
        """Test offset tracking."""
        settings = KafkaSettings()
        consumer = TaskConsumer(settings)

        consumer._track_offset("ftp-tasks", 0, 100)

        assert consumer._current_offsets[("ftp-tasks", 0)] == 101

    def test_get_stats(self) -> None:
        """Test consumer statistics."""
        settings = KafkaSettings(
            input_topic="ftp-tasks",
            consumer_group="test-group",
        )
        consumer = TaskConsumer(settings)

        stats = consumer.get_stats()

        assert stats["tasks_received"] == 0
        assert stats["parse_errors"] == 0
        assert stats["total_lag"] == 0
        assert stats["input_topic"] == "ftp-tasks"
        assert stats["consumer_group"] == "test-group"
        assert stats["running"] is False


class TestResultProducer:
    """Tests for ResultProducer."""

    def test_initial_state(self) -> None:
        """Test producer initial state."""
        settings = KafkaSettings(
            result_topic="ftp-results",
            fail_topic="ftp-failures",
        )
        producer = ResultProducer(settings)

        assert producer.results_sent == 0
        assert producer.failures_sent == 0
        assert producer.send_errors == 0
        assert producer.pending_count == 0

    def test_get_stats(self) -> None:
        """Test producer statistics."""
        settings = KafkaSettings(
            result_topic="ftp-results",
            fail_topic="ftp-failures",
        )
        producer = ResultProducer(settings)

        stats = producer.get_stats()

        assert stats["results_sent"] == 0
        assert stats["failures_sent"] == 0
        assert stats["send_errors"] == 0
        assert stats["pending_count"] == 0
        assert stats["result_topic"] == "ftp-results"
        assert stats["fail_topic"] == "ftp-failures"

    @pytest.mark.asyncio
    async def test_send_result_requires_start(self) -> None:
        """Test send_result requires producer to be started."""
        settings = KafkaSettings()
        producer = ResultProducer(settings)

        task = TransferTask(
            task_id="test-123",
            src_id="remote",
            src_path="/src",
            dst_id="local",
            dst_path="/dst",
        )
        result = TransferResult.success(task, 1024, 100)

        with pytest.raises(RuntimeError, match="not started"):
            await producer.send_result(result)


class TestKafkaSettings:
    """Tests for KafkaSettings."""

    def test_default_settings(self) -> None:
        """Test default Kafka settings."""
        settings = KafkaSettings()

        assert settings.bootstrap_servers == ["localhost:9092"]
        assert settings.consumer_group == "ftp-pooler"
        assert settings.input_topic == "ftp-tasks"
        assert settings.result_topic == "ftp-results"
        assert settings.fail_topic == "ftp-failures"
        assert settings.dlq_topic == "ftp-tasks-dlq"

    def test_retry_settings(self) -> None:
        """Test retry settings."""
        settings = KafkaSettings(
            max_retries=10,
            base_backoff_ms=500,
            max_backoff_ms=60000,
        )

        assert settings.max_retries == 10
        assert settings.base_backoff_ms == 500
        assert settings.max_backoff_ms == 60000
