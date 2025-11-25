"""Transfer task models."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
import uuid


class TransferStatus(str, Enum):
    """Transfer task status."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"


class TransferDirection(str, Enum):
    """Transfer direction."""

    DOWNLOAD = "download"  # remote -> local
    UPLOAD = "upload"  # local -> remote


@dataclass
class TransferTask:
    """A file transfer task."""

    task_id: str
    src_id: str
    src_path: str
    dst_id: str
    dst_path: str
    metadata: dict[str, Any] = field(default_factory=dict)
    status: TransferStatus = field(default=TransferStatus.PENDING)
    direction: Optional[TransferDirection] = field(default=None)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TransferTask":
        """Create a TransferTask from a dictionary.

        Args:
            data: Dictionary containing task data.

        Returns:
            TransferTask instance.

        Raises:
            ValueError: If required fields are missing.
        """
        required_fields = ["src_id", "src_path", "dst_id", "dst_path"]
        for field_name in required_fields:
            if field_name not in data:
                raise ValueError(f"Missing required field: {field_name}")

        return cls(
            task_id=data.get("task_id", str(uuid.uuid4())),
            src_id=data["src_id"],
            src_path=data["src_path"],
            dst_id=data["dst_id"],
            dst_path=data["dst_path"],
            metadata=data.get("metadata", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary.

        Returns:
            Dictionary representation of the task.
        """
        return {
            "task_id": self.task_id,
            "src_id": self.src_id,
            "src_path": self.src_path,
            "dst_id": self.dst_id,
            "dst_path": self.dst_path,
            "metadata": self.metadata,
            "status": self.status.value,
            "direction": self.direction.value if self.direction else None,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class TransferResult:
    """Result of a transfer task."""

    task_id: str
    status: TransferStatus
    src_id: str
    src_path: str
    dst_id: str
    dst_path: str
    bytes_transferred: int = 0
    duration_ms: int = 0
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def success(
        cls,
        task: TransferTask,
        bytes_transferred: int,
        duration_ms: int,
    ) -> "TransferResult":
        """Create a success result.

        Args:
            task: The completed task.
            bytes_transferred: Number of bytes transferred.
            duration_ms: Duration in milliseconds.

        Returns:
            TransferResult indicating success.
        """
        return cls(
            task_id=task.task_id,
            status=TransferStatus.SUCCESS,
            src_id=task.src_id,
            src_path=task.src_path,
            dst_id=task.dst_id,
            dst_path=task.dst_path,
            bytes_transferred=bytes_transferred,
            duration_ms=duration_ms,
        )

    @classmethod
    def failure(
        cls,
        task: TransferTask,
        error_code: str,
        error_message: str,
        duration_ms: int = 0,
    ) -> "TransferResult":
        """Create a failure result.

        Args:
            task: The failed task.
            error_code: Error code.
            error_message: Error message.
            duration_ms: Duration in milliseconds.

        Returns:
            TransferResult indicating failure.
        """
        return cls(
            task_id=task.task_id,
            status=TransferStatus.FAILED,
            src_id=task.src_id,
            src_path=task.src_path,
            dst_id=task.dst_id,
            dst_path=task.dst_path,
            duration_ms=duration_ms,
            error_code=error_code,
            error_message=error_message,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary.

        Returns:
            Dictionary representation of the result.
        """
        result = {
            "task_id": self.task_id,
            "status": self.status.value,
            "src_id": self.src_id,
            "src_path": self.src_path,
            "dst_id": self.dst_id,
            "dst_path": self.dst_path,
            "bytes_transferred": self.bytes_transferred,
            "duration_ms": self.duration_ms,
            "timestamp": self.timestamp.isoformat(),
        }

        if self.error_code:
            result["error_code"] = self.error_code
        if self.error_message:
            result["error_message"] = self.error_message

        return result
