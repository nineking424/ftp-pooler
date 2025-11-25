"""Prometheus metrics collector."""

from prometheus_client import Counter, Gauge, Histogram, Info, start_http_server
import structlog


logger = structlog.get_logger(__name__)


class MetricsCollector:
    """Collector for Prometheus metrics."""

    def __init__(self) -> None:
        """Initialize metrics."""
        # Application info
        self.info = Info(
            "ftp_pooler",
            "FTP Pooler application information",
        )
        self.info.info({
            "version": "0.1.0",
        })

        # Task counters
        self.tasks_total = Counter(
            "ftp_tasks_total",
            "Total number of transfer tasks processed",
            ["status"],
        )

        self.tasks_in_progress = Gauge(
            "ftp_tasks_in_progress",
            "Number of transfer tasks currently in progress",
        )

        # Transfer metrics
        self.transfer_duration = Histogram(
            "ftp_transfer_duration_seconds",
            "Transfer duration in seconds",
            ["direction", "status"],
            buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
        )

        self.transfer_bytes = Counter(
            "ftp_transfer_bytes_total",
            "Total bytes transferred",
            ["direction"],
        )

        # Session pool metrics
        self.sessions_active = Gauge(
            "ftp_sessions_active",
            "Number of active FTP sessions",
            ["connection_id"],
        )

        self.sessions_total = Gauge(
            "ftp_sessions_total",
            "Total number of FTP sessions",
            ["connection_id"],
        )

        self.session_pool_size = Gauge(
            "ftp_session_pool_size",
            "Maximum pool size",
        )

        # Kafka metrics
        self.kafka_messages_received = Counter(
            "ftp_kafka_messages_received_total",
            "Total Kafka messages received",
        )

        self.kafka_messages_sent = Counter(
            "ftp_kafka_messages_sent_total",
            "Total Kafka messages sent",
            ["topic_type"],  # result or fail
        )

        self.kafka_consumer_lag = Gauge(
            "ftp_kafka_consumer_lag",
            "Kafka consumer lag",
        )

        # Error metrics
        self.errors_total = Counter(
            "ftp_errors_total",
            "Total number of errors",
            ["error_code"],
        )

    def record_task_started(self) -> None:
        """Record a task has started."""
        self.tasks_in_progress.inc()

    def record_task_completed(
        self,
        direction: str,
        status: str,
        duration_seconds: float,
        bytes_transferred: int,
    ) -> None:
        """Record a task has completed.

        Args:
            direction: Transfer direction (upload/download).
            status: Task status (success/failed).
            duration_seconds: Duration in seconds.
            bytes_transferred: Number of bytes transferred.
        """
        self.tasks_in_progress.dec()
        self.tasks_total.labels(status=status).inc()
        self.transfer_duration.labels(
            direction=direction,
            status=status,
        ).observe(duration_seconds)

        if bytes_transferred > 0:
            self.transfer_bytes.labels(direction=direction).inc(bytes_transferred)

    def record_task_success(
        self,
        direction: str,
        duration_seconds: float,
        bytes_transferred: int,
    ) -> None:
        """Record a successful task.

        Args:
            direction: Transfer direction.
            duration_seconds: Duration in seconds.
            bytes_transferred: Number of bytes transferred.
        """
        self.record_task_completed(
            direction=direction,
            status="success",
            duration_seconds=duration_seconds,
            bytes_transferred=bytes_transferred,
        )

    def record_task_failure(
        self,
        direction: str,
        duration_seconds: float,
        error_code: str,
    ) -> None:
        """Record a failed task.

        Args:
            direction: Transfer direction.
            duration_seconds: Duration in seconds.
            error_code: Error code.
        """
        self.record_task_completed(
            direction=direction,
            status="failed",
            duration_seconds=duration_seconds,
            bytes_transferred=0,
        )
        self.errors_total.labels(error_code=error_code).inc()

    def update_session_metrics(
        self,
        connection_id: str,
        active: int,
        total: int,
    ) -> None:
        """Update session pool metrics.

        Args:
            connection_id: Connection ID.
            active: Number of active sessions.
            total: Total number of sessions.
        """
        self.sessions_active.labels(connection_id=connection_id).set(active)
        self.sessions_total.labels(connection_id=connection_id).set(total)

    def set_pool_size(self, size: int) -> None:
        """Set the maximum pool size.

        Args:
            size: Maximum pool size.
        """
        self.session_pool_size.set(size)

    def record_kafka_message_received(self) -> None:
        """Record a Kafka message was received."""
        self.kafka_messages_received.inc()

    def record_kafka_message_sent(self, topic_type: str) -> None:
        """Record a Kafka message was sent.

        Args:
            topic_type: Type of topic (result/fail).
        """
        self.kafka_messages_sent.labels(topic_type=topic_type).inc()

    def set_consumer_lag(self, lag: int) -> None:
        """Set the Kafka consumer lag.

        Args:
            lag: Consumer lag value.
        """
        self.kafka_consumer_lag.set(lag)


# Global metrics instance
_metrics: MetricsCollector | None = None


def get_metrics() -> MetricsCollector:
    """Get the global metrics collector.

    Returns:
        MetricsCollector instance.
    """
    global _metrics
    if _metrics is None:
        _metrics = MetricsCollector()
    return _metrics


def start_metrics_server(port: int = 9090) -> None:
    """Start the Prometheus metrics HTTP server.

    Args:
        port: Port to listen on.
    """
    start_http_server(port)
    logger.info("metrics_server_started", port=port)
