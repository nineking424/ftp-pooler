"""Dead Letter Queue producer for handling failed messages."""

import asyncio
import json
import random
from datetime import datetime, timezone
from typing import Optional

import structlog
from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError

from ftp_pooler.config.settings import KafkaSettings


logger = structlog.get_logger(__name__)


def _calculate_backoff(
    retry_count: int,
    base_backoff_ms: int,
    max_backoff_ms: int,
) -> float:
    """Calculate exponential backoff with jitter."""
    backoff_ms = base_backoff_ms * (2 ** (retry_count - 1))
    backoff_ms = min(backoff_ms, max_backoff_ms)
    jitter = backoff_ms * 0.2 * (random.random() * 2 - 1)
    backoff_ms = backoff_ms + jitter
    return backoff_ms / 1000.0


class DLQMessage:
    """Represents a message to be sent to the Dead Letter Queue."""

    def __init__(
        self,
        original_topic: str,
        original_partition: int,
        original_offset: int,
        original_key: Optional[bytes],
        original_value: bytes,
        error_type: str,
        error_message: str,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Initialize a DLQ message.

        Args:
            original_topic: The original topic name.
            original_partition: The original partition number.
            original_offset: The original message offset.
            original_key: The original message key.
            original_value: The original message value (raw bytes).
            error_type: Type of error that caused the failure.
            error_message: Detailed error message.
            timestamp: Timestamp of the failure.
        """
        self.original_topic = original_topic
        self.original_partition = original_partition
        self.original_offset = original_offset
        self.original_key = original_key
        self.original_value = original_value
        self.error_type = error_type
        self.error_message = error_message
        self.timestamp = timestamp or datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization.

        Returns:
            Dictionary representation of the DLQ message.
        """
        # Try to decode original value as string, otherwise base64 encode
        try:
            original_value_str = self.original_value.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            import base64
            original_value_str = base64.b64encode(self.original_value).decode("ascii")

        return {
            "original_topic": self.original_topic,
            "original_partition": self.original_partition,
            "original_offset": self.original_offset,
            "original_key": self.original_key.decode("utf-8") if self.original_key else None,
            "original_value": original_value_str,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "timestamp": self.timestamp.isoformat(),
        }


class DLQProducer:
    """Kafka producer for Dead Letter Queue messages."""

    def __init__(self, settings: KafkaSettings) -> None:
        """Initialize the DLQ producer.

        Args:
            settings: Kafka settings.
        """
        self._settings = settings
        self._producer: Optional[AIOKafkaProducer] = None
        self._messages_sent = 0

    @property
    def messages_sent(self) -> int:
        """Get number of messages sent to DLQ."""
        return self._messages_sent

    async def start(self) -> None:
        """Start the DLQ producer with retry logic.

        Raises:
            KafkaError: If connection fails after all retries.
        """
        if self._producer is not None:
            return

        retry_count = 0
        last_error: Optional[Exception] = None

        while retry_count <= self._settings.max_retries:
            try:
                self._producer = AIOKafkaProducer(
                    bootstrap_servers=",".join(self._settings.bootstrap_servers),
                    value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                    acks="all",
                    enable_idempotence=True,
                )

                await self._producer.start()

                logger.info(
                    "dlq_producer_started",
                    dlq_topic=self._settings.dlq_topic,
                    bootstrap_servers=self._settings.bootstrap_servers,
                )
                return

            except KafkaError as e:
                last_error = e
                retry_count += 1

                if retry_count > self._settings.max_retries:
                    logger.error(
                        "dlq_producer_start_failed",
                        error=str(e),
                        max_retries=self._settings.max_retries,
                    )
                    raise

                backoff = _calculate_backoff(
                    retry_count,
                    self._settings.base_backoff_ms,
                    self._settings.max_backoff_ms,
                )
                logger.warning(
                    "dlq_producer_start_retry",
                    error=str(e),
                    retry_count=retry_count,
                    backoff_seconds=round(backoff, 2),
                )
                await asyncio.sleep(backoff)

        if last_error:
            raise last_error

    async def stop(self) -> None:
        """Stop the DLQ producer."""
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

        logger.info("dlq_producer_stopped")

    async def send_to_dlq(self, dlq_message: DLQMessage) -> None:
        """Send a message to the Dead Letter Queue.

        Args:
            dlq_message: The DLQ message to send.

        Raises:
            RuntimeError: If producer is not started.
            KafkaError: If sending fails after all retries.
        """
        if self._producer is None:
            raise RuntimeError("DLQ producer is not started")

        retry_count = 0

        while retry_count <= self._settings.max_retries:
            try:
                await self._producer.send_and_wait(
                    topic=self._settings.dlq_topic,
                    value=dlq_message.to_dict(),
                    key=dlq_message.original_key,
                )

                self._messages_sent += 1

                logger.info(
                    "dlq_message_sent",
                    original_topic=dlq_message.original_topic,
                    original_partition=dlq_message.original_partition,
                    original_offset=dlq_message.original_offset,
                    error_type=dlq_message.error_type,
                )
                return

            except KafkaError as e:
                retry_count += 1

                if retry_count > self._settings.max_retries:
                    logger.error(
                        "dlq_send_error",
                        error=str(e),
                        original_offset=dlq_message.original_offset,
                        max_retries=self._settings.max_retries,
                    )
                    raise

                backoff = _calculate_backoff(
                    retry_count,
                    self._settings.base_backoff_ms,
                    self._settings.max_backoff_ms,
                )
                logger.warning(
                    "dlq_send_retry",
                    error=str(e),
                    retry_count=retry_count,
                    backoff_seconds=round(backoff, 2),
                )
                await asyncio.sleep(backoff)

    async def send_parse_error(
        self,
        message,
        error: Exception,
    ) -> None:
        """Helper to send a parsing error to DLQ.

        Args:
            message: The original Kafka message.
            error: The exception that caused the parse failure.
        """
        dlq_message = DLQMessage(
            original_topic=message.topic,
            original_partition=message.partition,
            original_offset=message.offset,
            original_key=message.key,
            original_value=message.value if isinstance(message.value, bytes) else str(message.value).encode(),
            error_type="PARSE_ERROR",
            error_message=str(error),
        )
        await self.send_to_dlq(dlq_message)

    def get_stats(self) -> dict:
        """Get DLQ producer statistics.

        Returns:
            Statistics dictionary.
        """
        return {
            "dlq_messages_sent": self._messages_sent,
            "dlq_topic": self._settings.dlq_topic,
        }
