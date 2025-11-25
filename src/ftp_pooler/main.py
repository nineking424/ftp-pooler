"""Main application entry point."""

import asyncio
import signal
import sys
from pathlib import Path
from typing import Optional

import structlog
import uvicorn

from ftp_pooler.api.routes import create_app
from ftp_pooler.config.connections import ConnectionRegistry, load_connections
from ftp_pooler.config.settings import Settings, get_settings
from ftp_pooler.kafka.consumer import TaskConsumer
from ftp_pooler.kafka.dlq import DLQProducer
from ftp_pooler.kafka.producer import ResultProducer
from ftp_pooler.logging import setup_logging
from ftp_pooler.pool.manager import SessionPoolManager
from ftp_pooler.transfer.engine import TransferEngine
from ftp_pooler.transfer.models import TransferResult, TransferTask


logger = structlog.get_logger(__name__)


class Application:
    """Main FTP Pooler application."""

    def __init__(self, settings: Settings) -> None:
        """Initialize the application.

        Args:
            settings: Application settings.
        """
        self._settings = settings
        self._connection_registry: Optional[ConnectionRegistry] = None
        self._pool_manager: Optional[SessionPoolManager] = None
        self._transfer_engine: Optional[TransferEngine] = None
        self._consumer: Optional[TaskConsumer] = None
        self._producer: Optional[ResultProducer] = None
        self._dlq_producer: Optional[DLQProducer] = None
        self._running = False
        self._shutdown_event = asyncio.Event()

    @property
    def is_running(self) -> bool:
        """Check if application is running."""
        return self._running

    async def _on_transfer_success(self, result: TransferResult) -> None:
        """Handle successful transfer.

        Args:
            result: Transfer result.
        """
        if self._producer:
            await self._producer.send_result(result)

    async def _on_transfer_failure(self, result: TransferResult) -> None:
        """Handle failed transfer.

        Args:
            result: Transfer result.
        """
        if self._producer:
            await self._producer.send_result(result)

    async def _process_task(self, task: TransferTask) -> None:
        """Process a single transfer task.

        Args:
            task: Transfer task to process.
        """
        if self._transfer_engine:
            await self._transfer_engine.execute(task)

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        loop = asyncio.get_running_loop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(self._handle_shutdown(s)),
            )

    async def _handle_shutdown(self, sig: signal.Signals) -> None:
        """Handle shutdown signal.

        Args:
            sig: Signal received.
        """
        logger.info("shutdown_signal_received", signal=sig.name)
        self._shutdown_event.set()

    async def initialize(self) -> None:
        """Initialize application components."""
        logger.info("initializing_application")

        # Load connection configurations
        if self._settings.connections_path:
            connections_path = Path(self._settings.connections_path)
            if connections_path.exists():
                self._connection_registry = load_connections(connections_path)
                logger.info(
                    "connections_loaded",
                    count=len(self._connection_registry),
                    path=str(connections_path),
                )
            else:
                logger.warning(
                    "connections_file_not_found",
                    path=str(connections_path),
                )
                self._connection_registry = ConnectionRegistry()
        else:
            self._connection_registry = ConnectionRegistry()
            logger.warning("no_connections_path_configured")

        # Initialize pool manager with circuit breaker
        self._pool_manager = SessionPoolManager(
            connection_registry=self._connection_registry,
            settings=self._settings.pool,
            circuit_breaker_settings=self._settings.circuit_breaker,
        )

        # Pre-warm connection pools
        prewarm_result = await self._pool_manager.warm_up_all()
        if prewarm_result.get("enabled"):
            logger.info(
                "pool_prewarm_result",
                total_created=prewarm_result.get("total_created", 0),
                total_failed=prewarm_result.get("total_failed", 0),
            )

        # Initialize Kafka producer
        self._producer = ResultProducer(self._settings.kafka)
        await self._producer.start()

        # Initialize DLQ producer
        self._dlq_producer = DLQProducer(self._settings.kafka)
        await self._dlq_producer.start()

        # Initialize transfer engine
        self._transfer_engine = TransferEngine(
            connection_registry=self._connection_registry,
            pool_manager=self._pool_manager,
            on_success=self._on_transfer_success,
            on_failure=self._on_transfer_failure,
            transfer_settings=self._settings.transfer,
        )

        # Initialize Kafka consumer with DLQ
        self._consumer = TaskConsumer(
            settings=self._settings.kafka,
            task_handler=self._process_task,
            dlq_producer=self._dlq_producer,
        )

        logger.info("application_initialized")

    async def start(self) -> None:
        """Start the application."""
        if self._running:
            return

        self._running = True
        self._setup_signal_handlers()

        logger.info("application_starting")

        if self._consumer:
            await self._consumer.start()

        logger.info("application_started")

    async def run(self) -> None:
        """Run the main application loop."""
        if not self._consumer:
            raise RuntimeError("Application not initialized")

        try:
            # Process tasks until shutdown
            async for task in self._consumer.consume():
                if self._shutdown_event.is_set():
                    break

                await self._process_task(task)

        except asyncio.CancelledError:
            logger.info("application_cancelled")
        except Exception as e:
            logger.exception("application_error", error=str(e))
            raise

    async def stop(self) -> None:
        """Stop the application."""
        if not self._running:
            return

        logger.info("application_stopping")

        self._running = False

        # Stop consumer
        if self._consumer:
            await self._consumer.stop()

        # Close all FTP sessions
        if self._pool_manager:
            await self._pool_manager.close_all()

        # Stop producer
        if self._producer:
            await self._producer.stop()

        # Stop DLQ producer
        if self._dlq_producer:
            await self._dlq_producer.stop()

        logger.info("application_stopped")

    async def _run_api_server(self) -> None:
        """Run the FastAPI server."""
        fastapi_app = create_app(self)
        config = uvicorn.Config(
            fastapi_app,
            host=self._settings.api.host,
            port=self._settings.api.port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        await server.serve()

    async def run_forever(self) -> None:
        """Run the application until shutdown signal."""
        await self.initialize()
        await self.start()

        # Start API server as background task
        api_task = asyncio.create_task(self._run_api_server())
        logger.info(
            "api_server_started",
            host=self._settings.api.host,
            port=self._settings.api.port,
        )

        try:
            await self.run()
        finally:
            api_task.cancel()
            try:
                await api_task
            except asyncio.CancelledError:
                pass
            await self.stop()

    def get_stats(self) -> dict:
        """Get application statistics.

        Returns:
            Statistics dictionary.
        """
        stats = {
            "running": self._running,
        }

        if self._pool_manager:
            stats["pool"] = self._pool_manager.get_stats()

        if self._consumer:
            stats["consumer"] = {
                "tasks_received": self._consumer.tasks_received,
                "parse_errors": self._consumer.parse_errors,
            }

        if self._producer:
            stats["producer"] = self._producer.get_stats()

        if self._dlq_producer:
            stats["dlq"] = self._dlq_producer.get_stats()

        if self._transfer_engine:
            stats["transfer"] = {
                "tasks_in_progress": self._transfer_engine.tasks_in_progress,
                "timeouts": self._transfer_engine.timeouts,
                "callback_errors": self._transfer_engine.callback_errors,
            }

        return stats


async def main() -> int:
    """Application entry point.

    Returns:
        Exit code.
    """
    # Load settings
    settings = get_settings()

    # Setup logging
    setup_logging(
        level=settings.logging.level,
        format=settings.logging.format,
    )

    logger.info(
        "ftp_pooler_starting",
        version="0.1.0",
        config_path=settings.config_path,
        connections_path=settings.connections_path,
    )

    # Create and run application
    app = Application(settings)

    try:
        await app.run_forever()
        return 0
    except Exception as e:
        logger.exception("fatal_error", error=str(e))
        return 1


def run() -> None:
    """Run the application."""
    exit_code = asyncio.run(main())
    sys.exit(exit_code)


if __name__ == "__main__":
    run()
