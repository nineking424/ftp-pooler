"""Transfer engine for executing file transfers."""

import asyncio
import time
from pathlib import Path
from typing import Callable, Optional, Awaitable

import structlog

from ftp_pooler.config.connections import (
    ConnectionRegistry,
    ConnectionType,
    LocalConnectionConfig,
)
from ftp_pooler.config.settings import TransferSettings
from ftp_pooler.pool.manager import SessionPoolManager
from ftp_pooler.transfer.models import (
    TransferDirection,
    TransferResult,
    TransferStatus,
    TransferTask,
)


logger = structlog.get_logger(__name__)


# Type alias for result callback
ResultCallback = Callable[[TransferResult], Awaitable[None]]


class TransferTimeoutError(Exception):
    """Raised when a transfer operation times out."""

    pass


class TransferEngine:
    """Engine for executing file transfer tasks."""

    def __init__(
        self,
        connection_registry: ConnectionRegistry,
        pool_manager: SessionPoolManager,
        on_success: Optional[ResultCallback] = None,
        on_failure: Optional[ResultCallback] = None,
        transfer_settings: Optional[TransferSettings] = None,
    ) -> None:
        """Initialize the transfer engine.

        Args:
            connection_registry: Registry of connection configurations.
            pool_manager: FTP session pool manager.
            on_success: Optional callback for successful transfers.
            on_failure: Optional callback for failed transfers.
            transfer_settings: Optional transfer timeout settings.
        """
        self._registry = connection_registry
        self._pool_manager = pool_manager
        self._on_success = on_success
        self._on_failure = on_failure
        self._transfer_settings = transfer_settings or TransferSettings()
        self._tasks_in_progress = 0
        self._lock = asyncio.Lock()
        self._timeouts = 0

    @property
    def tasks_in_progress(self) -> int:
        """Get number of tasks currently in progress."""
        return self._tasks_in_progress

    @property
    def timeouts(self) -> int:
        """Get number of transfers that timed out."""
        return self._timeouts

    def _determine_direction(self, task: TransferTask) -> TransferDirection:
        """Determine the transfer direction.

        Args:
            task: The transfer task.

        Returns:
            Transfer direction.

        Raises:
            ValueError: If both or neither endpoints are local.
        """
        src_config = self._registry.get(task.src_id)
        dst_config = self._registry.get(task.dst_id)

        src_is_local = src_config.type == ConnectionType.LOCAL
        dst_is_local = dst_config.type == ConnectionType.LOCAL

        if src_is_local and dst_is_local:
            raise ValueError(
                f"Both source and destination are local: "
                f"{task.src_id} -> {task.dst_id}"
            )

        if not src_is_local and not dst_is_local:
            raise ValueError(
                f"Neither source nor destination is local: "
                f"{task.src_id} -> {task.dst_id}"
            )

        return TransferDirection.UPLOAD if src_is_local else TransferDirection.DOWNLOAD

    def _resolve_local_path(
        self,
        connection_id: str,
        file_path: str,
    ) -> Path:
        """Resolve a file path for a local connection.

        Args:
            connection_id: The local connection ID.
            file_path: The file path.

        Returns:
            Resolved absolute path.
        """
        config = self._registry.get_local(connection_id)
        base_path = Path(config.base_path)

        # Remove leading slash from file_path for proper joining
        if file_path.startswith("/"):
            file_path = file_path[1:]

        return base_path / file_path

    async def _execute_download(
        self,
        task: TransferTask,
    ) -> tuple[int, int]:
        """Execute a download transfer with timeout.

        Args:
            task: The transfer task.

        Returns:
            Tuple of (bytes_transferred, duration_ms).

        Raises:
            TransferTimeoutError: If the transfer times out.
        """
        local_path = self._resolve_local_path(task.dst_id, task.dst_path)
        timeout_seconds = self._transfer_settings.transfer_timeout_seconds

        start_time = time.monotonic()

        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._pool_manager.acquire(task.src_id) as session:
                    bytes_transferred = await session.download(
                        remote_path=task.src_path,
                        local_path=local_path,
                    )
        except asyncio.TimeoutError:
            raise TransferTimeoutError(
                f"Download timed out after {timeout_seconds}s: "
                f"{task.src_path} -> {task.dst_path}"
            )

        duration_ms = int((time.monotonic() - start_time) * 1000)
        return bytes_transferred, duration_ms

    async def _execute_upload(
        self,
        task: TransferTask,
    ) -> tuple[int, int]:
        """Execute an upload transfer with timeout.

        Args:
            task: The transfer task.

        Returns:
            Tuple of (bytes_transferred, duration_ms).

        Raises:
            TransferTimeoutError: If the transfer times out.
        """
        local_path = self._resolve_local_path(task.src_id, task.src_path)
        timeout_seconds = self._transfer_settings.transfer_timeout_seconds

        start_time = time.monotonic()

        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._pool_manager.acquire(task.dst_id) as session:
                    bytes_transferred = await session.upload(
                        local_path=local_path,
                        remote_path=task.dst_path,
                    )
        except asyncio.TimeoutError:
            raise TransferTimeoutError(
                f"Upload timed out after {timeout_seconds}s: "
                f"{task.src_path} -> {task.dst_path}"
            )

        duration_ms = int((time.monotonic() - start_time) * 1000)
        return bytes_transferred, duration_ms

    async def execute(self, task: TransferTask) -> TransferResult:
        """Execute a transfer task.

        Args:
            task: The transfer task to execute.

        Returns:
            Transfer result.
        """
        async with self._lock:
            self._tasks_in_progress += 1

        log = logger.bind(
            task_id=task.task_id,
            src_id=task.src_id,
            src_path=task.src_path,
            dst_id=task.dst_id,
            dst_path=task.dst_path,
        )

        start_time = time.monotonic()

        try:
            # Determine transfer direction
            direction = self._determine_direction(task)
            task.direction = direction
            task.status = TransferStatus.IN_PROGRESS

            log.info("transfer_started", direction=direction.value)

            # Execute transfer based on direction
            if direction == TransferDirection.DOWNLOAD:
                bytes_transferred, duration_ms = await self._execute_download(task)
            else:
                bytes_transferred, duration_ms = await self._execute_upload(task)

            # Create success result
            result = TransferResult.success(
                task=task,
                bytes_transferred=bytes_transferred,
                duration_ms=duration_ms,
            )

            log.info(
                "transfer_completed",
                direction=direction.value,
                bytes_transferred=bytes_transferred,
                duration_ms=duration_ms,
            )

            # Call success callback
            if self._on_success:
                await self._on_success(result)

            return result

        except ValueError as e:
            # Configuration error
            duration_ms = int((time.monotonic() - start_time) * 1000)
            result = TransferResult.failure(
                task=task,
                error_code="INVALID_CONFIG",
                error_message=str(e),
                duration_ms=duration_ms,
            )
            log.error("transfer_failed", error_code="INVALID_CONFIG", error=str(e))

        except FileNotFoundError as e:
            # Local file not found
            duration_ms = int((time.monotonic() - start_time) * 1000)
            result = TransferResult.failure(
                task=task,
                error_code="FILE_NOT_FOUND",
                error_message=str(e),
                duration_ms=duration_ms,
            )
            log.error("transfer_failed", error_code="FILE_NOT_FOUND", error=str(e))

        except ConnectionError as e:
            # FTP connection error
            duration_ms = int((time.monotonic() - start_time) * 1000)
            result = TransferResult.failure(
                task=task,
                error_code="CONNECTION_ERROR",
                error_message=str(e),
                duration_ms=duration_ms,
            )
            log.error("transfer_failed", error_code="CONNECTION_ERROR", error=str(e))

        except IOError as e:
            # Transfer IO error
            duration_ms = int((time.monotonic() - start_time) * 1000)
            result = TransferResult.failure(
                task=task,
                error_code="IO_ERROR",
                error_message=str(e),
                duration_ms=duration_ms,
            )
            log.error("transfer_failed", error_code="IO_ERROR", error=str(e))

        except TransferTimeoutError as e:
            # Transfer timeout
            self._timeouts += 1
            duration_ms = int((time.monotonic() - start_time) * 1000)
            result = TransferResult.failure(
                task=task,
                error_code="TRANSFER_TIMEOUT",
                error_message=str(e),
                duration_ms=duration_ms,
            )
            log.error(
                "transfer_failed",
                error_code="TRANSFER_TIMEOUT",
                error=str(e),
                timeout_seconds=self._transfer_settings.transfer_timeout_seconds,
            )

        except Exception as e:
            # Unexpected error
            duration_ms = int((time.monotonic() - start_time) * 1000)
            result = TransferResult.failure(
                task=task,
                error_code="UNKNOWN_ERROR",
                error_message=str(e),
                duration_ms=duration_ms,
            )
            log.exception("transfer_failed", error_code="UNKNOWN_ERROR")

        finally:
            async with self._lock:
                self._tasks_in_progress -= 1

        # Call failure callback
        if self._on_failure:
            await self._on_failure(result)

        return result

    async def execute_batch(
        self,
        tasks: list[TransferTask],
        max_concurrent: int = 10,
    ) -> list[TransferResult]:
        """Execute multiple transfer tasks concurrently.

        Args:
            tasks: List of transfer tasks.
            max_concurrent: Maximum number of concurrent transfers.

        Returns:
            List of transfer results.
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def execute_with_semaphore(task: TransferTask) -> TransferResult:
            async with semaphore:
                return await self.execute(task)

        results = await asyncio.gather(
            *[execute_with_semaphore(task) for task in tasks],
            return_exceptions=False,
        )

        return results
