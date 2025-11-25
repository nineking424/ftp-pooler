"""Kafka consumer for receiving transfer tasks."""

import asyncio
import json
from typing import AsyncIterator, Callable, Optional, Awaitable

import structlog
from aiokafka import AIOKafkaConsumer
from aiokafka.errors import KafkaError

from ftp_pooler.config.settings import KafkaSettings
from ftp_pooler.transfer.models import TransferTask


logger = structlog.get_logger(__name__)


def _safe_json_deserialize(data: bytes) -> Optional[dict]:
    """Safely deserialize JSON data.

    Args:
        data: Raw bytes from Kafka message.

    Returns:
        Parsed JSON dict or None if parsing fails.
    """
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# Type alias for task handler
TaskHandler = Callable[[TransferTask], Awaitable[None]]


class TaskConsumer:
    """Kafka consumer for transfer tasks."""

    def __init__(
        self,
        settings: KafkaSettings,
        task_handler: Optional[TaskHandler] = None,
    ) -> None:
        """Initialize the task consumer.

        Args:
            settings: Kafka settings.
            task_handler: Optional handler for incoming tasks.
        """
        self._settings = settings
        self._task_handler = task_handler
        self._consumer: Optional[AIOKafkaConsumer] = None
        self._running = False
        self._tasks_received = 0

    @property
    def is_running(self) -> bool:
        """Check if consumer is running."""
        return self._running

    @property
    def tasks_received(self) -> int:
        """Get number of tasks received."""
        return self._tasks_received

    async def start(self) -> None:
        """Start the Kafka consumer.

        Raises:
            KafkaError: If connection fails.
        """
        if self._consumer is not None:
            return

        self._consumer = AIOKafkaConsumer(
            self._settings.input_topic,
            bootstrap_servers=",".join(self._settings.bootstrap_servers),
            group_id=self._settings.consumer_group,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            value_deserializer=_safe_json_deserialize,
        )

        await self._consumer.start()
        self._running = True

        logger.info(
            "consumer_started",
            topic=self._settings.input_topic,
            group_id=self._settings.consumer_group,
            bootstrap_servers=self._settings.bootstrap_servers,
        )

    async def stop(self) -> None:
        """Stop the Kafka consumer."""
        self._running = False

        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None

        logger.info("consumer_stopped")

    async def _parse_message(self, message) -> Optional[TransferTask]:
        """Parse a Kafka message into a TransferTask.

        Args:
            message: Kafka message.

        Returns:
            TransferTask or None if parsing fails.
        """
        try:
            data = message.value
            if data is None:
                logger.warning(
                    "empty_message_skipped",
                    offset=message.offset,
                    partition=message.partition,
                )
                return None

            if isinstance(data, str):
                data = json.loads(data)

            task = TransferTask.from_dict(data)
            return task

        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            logger.error(
                "message_parse_error",
                error=str(e),
                offset=message.offset,
                partition=message.partition,
            )
            return None

    async def consume(self) -> AsyncIterator[TransferTask]:
        """Consume tasks from Kafka.

        Yields:
            TransferTask instances.

        Raises:
            RuntimeError: If consumer is not started.
        """
        if self._consumer is None:
            raise RuntimeError("Consumer is not started")

        try:
            async for message in self._consumer:
                task = await self._parse_message(message)

                if task is not None:
                    self._tasks_received += 1

                    logger.debug(
                        "task_received",
                        task_id=task.task_id,
                        src_id=task.src_id,
                        dst_id=task.dst_id,
                        offset=message.offset,
                        partition=message.partition,
                    )

                    yield task

                # Commit offset after processing
                await self._consumer.commit()

        except KafkaError as e:
            logger.error("kafka_consumer_error", error=str(e))
            raise

    async def run(self) -> None:
        """Run the consumer loop with the configured handler.

        Raises:
            RuntimeError: If no task handler is configured.
        """
        if self._task_handler is None:
            raise RuntimeError("No task handler configured")

        await self.start()

        try:
            async for task in self.consume():
                try:
                    await self._task_handler(task)
                except Exception as e:
                    logger.error(
                        "task_handler_error",
                        task_id=task.task_id,
                        error=str(e),
                    )
        finally:
            await self.stop()

    async def consume_batch(
        self,
        batch_size: int = 100,
        timeout_ms: int = 1000,
    ) -> list[TransferTask]:
        """Consume a batch of tasks from Kafka.

        Args:
            batch_size: Maximum number of tasks to consume.
            timeout_ms: Timeout in milliseconds.

        Returns:
            List of TransferTask instances.

        Raises:
            RuntimeError: If consumer is not started.
        """
        if self._consumer is None:
            raise RuntimeError("Consumer is not started")

        tasks: list[TransferTask] = []

        try:
            # Get batch of messages
            messages = await self._consumer.getmany(
                timeout_ms=timeout_ms,
                max_records=batch_size,
            )

            for topic_partition, partition_messages in messages.items():
                for message in partition_messages:
                    task = await self._parse_message(message)
                    if task is not None:
                        self._tasks_received += 1
                        tasks.append(task)

            # Commit after batch
            if tasks:
                await self._consumer.commit()

                logger.debug(
                    "batch_consumed",
                    count=len(tasks),
                )

        except KafkaError as e:
            logger.error("kafka_batch_error", error=str(e))
            raise

        return tasks
