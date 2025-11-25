"""Kafka producer for sending transfer results."""

import asyncio
import json
import random
from typing import Optional, Set

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
    """Kafka producer for transfer results with async batching support."""

    def __init__(self, settings: KafkaSettings) -> None:
        """Initialize the result producer.

        Args:
            settings: Kafka settings.
        """
        self._settings = settings
        self._producer: Optional[AIOKafkaProducer] = None
        self._results_sent = 0
        self._failures_sent = 0
        self._send_errors = 0
        self._pending_tasks: Set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    @property
    def results_sent(self) -> int:
        """Get number of results sent."""
        return self._results_sent

    @property
    def failures_sent(self) -> int:
        """Get number of failures sent."""
        return self._failures_sent

    @property
    def send_errors(self) -> int:
        """Get number of send errors."""
        return self._send_errors

    @property
    def pending_count(self) -> int:
        """Get number of pending send tasks."""
        return len(self._pending_tasks)

    async def start(self) -> None:
        """Start the Kafka producer with retry logic and batching config.

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
                    # Batching configuration for better throughput
                    linger_ms=10,  # Wait up to 10ms for batching
                    batch_size=16384,  # 16KB batch size
                    max_request_size=1048576,  # 1MB max request
                    compression_type="gzip",  # Compress messages
                )

                await self._producer.start()

                logger.info(
                    "producer_started",
                    bootstrap_servers=self._settings.bootstrap_servers,
                    retries=retry_count,
                    linger_ms=10,
                    batch_size=16384,
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
        """Stop the Kafka producer, flushing pending messages."""
        # Wait for pending tasks to complete
        if self._pending_tasks:
            logger.info(
                "producer_flushing_pending",
                pending_count=len(self._pending_tasks),
            )
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            self._pending_tasks.clear()

        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

        logger.info("producer_stopped")

    async def flush(self, timeout: float = 10.0) -> int:
        """Flush all pending messages.

        Args:
            timeout: Maximum time to wait for flush.

        Returns:
            Number of messages flushed.
        """
        if not self._pending_tasks:
            return 0

        pending_count = len(self._pending_tasks)
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._pending_tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "producer_flush_timeout",
                pending_count=len(self._pending_tasks),
                timeout=timeout,
            )

        flushed = pending_count - len(self._pending_tasks)
        self._pending_tasks.clear()
        return flushed

    def _task_done_callback(self, task: asyncio.Task) -> None:
        """Callback when a send task completes."""
        self._pending_tasks.discard(task)

    async def _send_with_retry(
        self,
        topic: str,
        value: dict,
        key: bytes,
        result: TransferResult,
    ) -> None:
        """Send a message with retry logic.

        Args:
            topic: Kafka topic.
            value: Message value.
            key: Message key.
            result: Original transfer result for logging.
        """
        retry_count = 0

        while retry_count <= self._settings.max_retries:
            try:
                await self._producer.send_and_wait(
                    topic=topic,
                    value=value,
                    key=key,
                )

                # Update counters only on success
                async with self._lock:
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
                    async with self._lock:
                        self._send_errors += 1
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

    async def send_result(self, result: TransferResult, wait: bool = True) -> None:
        """Send a transfer result to Kafka.

        Args:
            result: Transfer result to send.
            wait: If True, wait for send to complete. If False, fire-and-forget.

        Raises:
            RuntimeError: If producer is not started.
            KafkaError: If sending fails after all retries (only when wait=True).
        """
        if self._producer is None:
            raise RuntimeError("Producer is not started")

        # Determine topic based on result status
        if result.status == TransferStatus.SUCCESS:
            topic = self._settings.result_topic
        else:
            topic = self._settings.fail_topic

        if wait:
            # Synchronous send with retry
            await self._send_with_retry(
                topic=topic,
                value=result.to_dict(),
                key=result.task_id.encode("utf-8"),
                result=result,
            )
        else:
            # Fire-and-forget mode
            task = asyncio.create_task(
                self._send_with_retry(
                    topic=topic,
                    value=result.to_dict(),
                    key=result.task_id.encode("utf-8"),
                    result=result,
                )
            )
            task.add_done_callback(self._task_done_callback)
            self._pending_tasks.add(task)

            logger.debug(
                "result_queued",
                task_id=result.task_id,
                status=result.status.value,
                topic=topic,
                pending_count=len(self._pending_tasks),
            )

    async def send_result_async(self, result: TransferResult) -> None:
        """Send a transfer result asynchronously (fire-and-forget).

        This is a convenience method for non-blocking sends.

        Args:
            result: Transfer result to send.

        Raises:
            RuntimeError: If producer is not started.
        """
        await self.send_result(result, wait=False)

    async def send_batch(
        self,
        results: list[TransferResult],
        wait: bool = True,
    ) -> int:
        """Send multiple transfer results in batch.

        Args:
            results: List of transfer results to send.
            wait: If True, wait for all sends to complete.

        Returns:
            Number of results queued/sent.

        Raises:
            RuntimeError: If producer is not started.
        """
        if self._producer is None:
            raise RuntimeError("Producer is not started")

        tasks = []
        for result in results:
            topic = (
                self._settings.result_topic
                if result.status == TransferStatus.SUCCESS
                else self._settings.fail_topic
            )

            task = asyncio.create_task(
                self._send_with_retry(
                    topic=topic,
                    value=result.to_dict(),
                    key=result.task_id.encode("utf-8"),
                    result=result,
                )
            )
            task.add_done_callback(self._task_done_callback)
            self._pending_tasks.add(task)
            tasks.append(task)

        logger.info(
            "batch_queued",
            batch_size=len(results),
            pending_count=len(self._pending_tasks),
        )

        if wait:
            await asyncio.gather(*tasks, return_exceptions=True)

        return len(results)

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
            "send_errors": self._send_errors,
            "pending_count": len(self._pending_tasks),
            "result_topic": self._settings.result_topic,
            "fail_topic": self._settings.fail_topic,
        }
