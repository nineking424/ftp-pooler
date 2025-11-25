"""Kafka producer for sending transfer results."""

import json
from typing import Optional

import structlog
from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError

from ftp_pooler.config.settings import KafkaSettings
from ftp_pooler.transfer.models import TransferResult, TransferStatus


logger = structlog.get_logger(__name__)


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
        """Start the Kafka producer.

        Raises:
            KafkaError: If connection fails.
        """
        if self._producer is not None:
            return

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
        )

    async def stop(self) -> None:
        """Stop the Kafka producer."""
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

        logger.info("producer_stopped")

    async def send_result(self, result: TransferResult) -> None:
        """Send a transfer result to Kafka.

        Args:
            result: Transfer result to send.

        Raises:
            RuntimeError: If producer is not started.
            KafkaError: If sending fails.
        """
        if self._producer is None:
            raise RuntimeError("Producer is not started")

        # Determine topic based on result status
        if result.status == TransferStatus.SUCCESS:
            topic = self._settings.result_topic
            self._results_sent += 1
        else:
            topic = self._settings.fail_topic
            self._failures_sent += 1

        try:
            await self._producer.send_and_wait(
                topic=topic,
                value=result.to_dict(),
                key=result.task_id.encode("utf-8"),
            )

            logger.debug(
                "result_sent",
                task_id=result.task_id,
                status=result.status.value,
                topic=topic,
            )

        except KafkaError as e:
            logger.error(
                "result_send_error",
                task_id=result.task_id,
                error=str(e),
            )
            raise

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
