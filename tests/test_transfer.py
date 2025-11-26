"""Tests for transfer module."""

import pytest

from ftp_pooler.config.settings import TransferSettings
from ftp_pooler.transfer.engine import TransferTimeoutError
from ftp_pooler.transfer.models import (
    TransferDirection,
    TransferResult,
    TransferStatus,
    TransferTask,
)


class TestTransferTask:
    """Tests for TransferTask."""

    def test_create_from_dict(self) -> None:
        """Test creating task from dictionary."""
        data = {
            "task_id": "test-123",
            "src_id": "remote-server",
            "src_path": "/data/file.txt",
            "dst_id": "local",
            "dst_path": "/storage/file.txt",
            "metadata": {"key": "value"},
        }

        task = TransferTask.from_dict(data)

        assert task.task_id == "test-123"
        assert task.src_id == "remote-server"
        assert task.src_path == "/data/file.txt"
        assert task.dst_id == "local"
        assert task.dst_path == "/storage/file.txt"
        assert task.metadata == {"key": "value"}
        assert task.status == TransferStatus.PENDING

    def test_create_from_dict_generates_id(self) -> None:
        """Test that task_id is generated if not provided."""
        data = {
            "src_id": "remote",
            "src_path": "/src",
            "dst_id": "local",
            "dst_path": "/dst",
        }

        task = TransferTask.from_dict(data)

        assert task.task_id is not None
        assert len(task.task_id) > 0

    def test_create_from_dict_missing_field_raises(self) -> None:
        """Test that missing required field raises error."""
        data = {
            "src_id": "remote",
            # missing src_path
            "dst_id": "local",
            "dst_path": "/dst",
        }

        with pytest.raises(ValueError, match="Missing required field"):
            TransferTask.from_dict(data)

    def test_to_dict(self) -> None:
        """Test converting task to dictionary."""
        task = TransferTask(
            task_id="test-123",
            src_id="remote",
            src_path="/src/file.txt",
            dst_id="local",
            dst_path="/dst/file.txt",
        )

        data = task.to_dict()

        assert data["task_id"] == "test-123"
        assert data["src_id"] == "remote"
        assert data["src_path"] == "/src/file.txt"
        assert data["dst_id"] == "local"
        assert data["dst_path"] == "/dst/file.txt"
        assert data["status"] == "pending"


class TestTransferResult:
    """Tests for TransferResult."""

    def test_success_result(self) -> None:
        """Test creating success result."""
        task = TransferTask(
            task_id="test-123",
            src_id="remote",
            src_path="/src/file.txt",
            dst_id="local",
            dst_path="/dst/file.txt",
        )

        result = TransferResult.success(
            task=task,
            bytes_transferred=1024,
            duration_ms=500,
        )

        assert result.task_id == "test-123"
        assert result.status == TransferStatus.SUCCESS
        assert result.bytes_transferred == 1024
        assert result.duration_ms == 500
        assert result.error_code is None
        assert result.error_message is None

    def test_failure_result(self) -> None:
        """Test creating failure result."""
        task = TransferTask(
            task_id="test-123",
            src_id="remote",
            src_path="/src/file.txt",
            dst_id="local",
            dst_path="/dst/file.txt",
        )

        result = TransferResult.failure(
            task=task,
            error_code="CONNECTION_ERROR",
            error_message="Failed to connect",
            duration_ms=100,
        )

        assert result.task_id == "test-123"
        assert result.status == TransferStatus.FAILED
        assert result.error_code == "CONNECTION_ERROR"
        assert result.error_message == "Failed to connect"
        assert result.duration_ms == 100

    def test_to_dict_success(self) -> None:
        """Test converting success result to dictionary."""
        task = TransferTask(
            task_id="test-123",
            src_id="remote",
            src_path="/src/file.txt",
            dst_id="local",
            dst_path="/dst/file.txt",
        )

        result = TransferResult.success(
            task=task,
            bytes_transferred=2048,
            duration_ms=1000,
        )

        data = result.to_dict()

        assert data["task_id"] == "test-123"
        assert data["status"] == "success"
        assert data["bytes_transferred"] == 2048
        assert data["duration_ms"] == 1000
        assert "error_code" not in data
        assert "error_message" not in data

    def test_to_dict_failure(self) -> None:
        """Test converting failure result to dictionary."""
        task = TransferTask(
            task_id="test-123",
            src_id="remote",
            src_path="/src/file.txt",
            dst_id="local",
            dst_path="/dst/file.txt",
        )

        result = TransferResult.failure(
            task=task,
            error_code="IO_ERROR",
            error_message="Disk full",
        )

        data = result.to_dict()

        assert data["task_id"] == "test-123"
        assert data["status"] == "failed"
        assert data["error_code"] == "IO_ERROR"
        assert data["error_message"] == "Disk full"


class TestTransferEnums:
    """Tests for transfer enums."""

    def test_transfer_status_values(self) -> None:
        """Test TransferStatus enum values."""
        assert TransferStatus.PENDING.value == "pending"
        assert TransferStatus.IN_PROGRESS.value == "in_progress"
        assert TransferStatus.SUCCESS.value == "success"
        assert TransferStatus.FAILED.value == "failed"

    def test_transfer_direction_values(self) -> None:
        """Test TransferDirection enum values."""
        assert TransferDirection.DOWNLOAD.value == "download"
        assert TransferDirection.UPLOAD.value == "upload"


class TestTransferSettings:
    """Tests for TransferSettings."""

    def test_default_settings(self) -> None:
        """Test default transfer settings."""
        settings = TransferSettings()

        assert settings.transfer_timeout_seconds == 300
        assert settings.connect_timeout_seconds == 30

    def test_custom_settings(self) -> None:
        """Test custom transfer settings."""
        settings = TransferSettings(
            transfer_timeout_seconds=600,
            connect_timeout_seconds=60,
        )

        assert settings.transfer_timeout_seconds == 600
        assert settings.connect_timeout_seconds == 60


class TestTransferTimeoutError:
    """Tests for TransferTimeoutError exception."""

    def test_error_message(self) -> None:
        """Test timeout error message."""
        error = TransferTimeoutError("Download timed out after 300s")

        assert "timed out" in str(error)
        assert "300s" in str(error)


class TestTransferResultWithTimeout:
    """Tests for TransferResult with timeout error code."""

    def test_timeout_failure_result(self) -> None:
        """Test creating failure result with TRANSFER_TIMEOUT error code."""
        task = TransferTask(
            task_id="test-timeout",
            src_id="remote",
            src_path="/large/file.bin",
            dst_id="local",
            dst_path="/dst/file.bin",
        )

        result = TransferResult.failure(
            task=task,
            error_code="TRANSFER_TIMEOUT",
            error_message="Transfer timed out after 300s",
            duration_ms=300000,
        )

        assert result.status == TransferStatus.FAILED
        assert result.error_code == "TRANSFER_TIMEOUT"
        assert "300s" in result.error_message
        assert result.duration_ms == 300000

    def test_to_dict_includes_timeout_error(self) -> None:
        """Test to_dict includes timeout error details."""
        task = TransferTask(
            task_id="test-timeout",
            src_id="remote",
            src_path="/src",
            dst_id="local",
            dst_path="/dst",
        )

        result = TransferResult.failure(
            task=task,
            error_code="TRANSFER_TIMEOUT",
            error_message="Operation timed out",
        )

        data = result.to_dict()

        assert data["status"] == "failed"
        assert data["error_code"] == "TRANSFER_TIMEOUT"
        assert data["error_message"] == "Operation timed out"
