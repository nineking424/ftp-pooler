"""Kafka producer for sending transfer results."""

import asyncio
import json
import random
from typing import Optional

import structlog
from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError

from ftp_pooler.config.settings import KafkaSettings
from ftp_pooler.transfer.models import TransferResult, TransferStatus


logger = structlog.get_logger(__name__)


def _calculate_backoff(
    retry_count: int,
    base_backoff_ms: int,
    max_backoff_ms: int,
) -> float:
    """Calculate exponential backoff with jitter.

    Args:
        retry_count: Current retry attempt (1-based).
        base_backoff_ms: Base backoff in milliseconds.
        max_backoff_ms: Maximum backoff in milliseconds.

    Returns:
        Backoff time in seconds.
    """
    backoff_ms = base_backoff_ms * (2 ** (retry_count - 1))
    backoff_ms = min(backoff_ms, max_backoff_ms)
    jitter = backoff_ms * 0.2 * (random.random() * 2 - 1)
    backoff_ms = backoff_ms + jitter
    return backoff_ms / 1000.0


class ResultProducer:
    """Kafka producer for transfer results."""

    def __init__(self, settings: KafkaSettings) -> None:
        """Initialize the result producer.

        Args:
            settings: Kafka settings.
        """
        self._settings = settings
        self._producer: Optional[AIOKafkaProducer] = None
        self._results_sent = 0
        self._failures_sent = 0

    @property
    def results_sent(self) -> int:
        """Get number of results sent."""
        return self._results_sent

    @property
    def failures_sent(self) -> int:
        """Get number of failures sent."""
        return self._failures_sent

    async def start(self) -> None:
        """Start the Kafka producer with retry logic.

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
                    "producer_started",
                    bootstrap_servers=self._settings.bootstrap_servers,
                    retries=retry_count,
                )
                return

            except KafkaError as e:
                last_error = e
                retry_count += 1

                if retry_count > self._settings.max_retries:
                    logger.error(
                        "producer_start_failed",
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
                    "producer_start_retry",
                    error=str(e),
                    retry_count=retry_count,
                    max_retries=self._settings.max_retries,
                    backoff_seconds=round(backoff, 2),
                )
                await asyncio.sleep(backoff)

        if last_error:
            raise last_error

    async def stop(self) -> None:
        """Stop the Kafka producer."""
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

        logger.info("producer_stopped")

    async def send_result(self, result: TransferResult) -> None:
        """Send a transfer result to Kafka with retry logic.

        Args:
            result: Transfer result to send.

        Raises:
            RuntimeError: If producer is not started.
            KafkaError: If sending fails after all retries.
        """
        if self._producer is None:
            raise RuntimeError("Producer is not started")

        # Determine topic based on result status
        if result.status == TransferStatus.SUCCESS:
            topic = self._settings.result_topic
        else:
            topic = self._settings.fail_topic

        retry_count = 0

        while retry_count <= self._settings.max_retries:
            try:
                await self._producer.send_and_wait(
                    topic=topic,
                    value=result.to_dict(),
                    key=result.task_id.encode("utf-8"),
                )

                # Update counters only on success
                if result.status == TransferStatus.SUCCESS:
                    self._results_sent += 1
                else:
                    self._failures_sent += 1

                logger.debug(
                    "result_sent",
                    task_id=result.task_id,
                    status=result.status.value,
                    topic=topic,
                    retries=retry_count,
                )
                return

            except KafkaError as e:
                retry_count += 1

                if retry_count > self._settings.max_retries:
                    logger.error(
                        "result_send_error",
                        task_id=result.task_id,
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
                    "result_send_retry",
                    task_id=result.task_id,
                    error=str(e),
                    retry_count=retry_count,
                    max_retries=self._settings.max_retries,
                    backoff_seconds=round(backoff, 2),
                )
                await asyncio.sleep(backoff)

    async def send_success(self, result: TransferResult) -> None:
        """Send a success result to the result topic.

        Args:
            result: Success result to send.
        """
        if result.status != TransferStatus.SUCCESS:
            logger.warning(
                "send_success_wrong_status",
                task_id=result.task_id,
                actual_status=result.status.value,
            )

        await self.send_result(result)

    async def send_failure(self, result: TransferResult) -> None:
        """Send a failure result to the failure topic.

        Args:
            result: Failure result to send.
        """
        if result.status != TransferStatus.FAILED:
            logger.warning(
                "send_failure_wrong_status",
                task_id=result.task_id,
                actual_status=result.status.value,
            )

        await self.send_result(result)

    def get_stats(self) -> dict:
        """Get producer statistics.

        Returns:
            Statistics dictionary.
        """
        return {
            "results_sent": self._results_sent,
            "failures_sent": self._failures_sent,
            "result_topic": self._settings.result_topic,
            "fail_topic": self._settings.fail_topic,
        }
