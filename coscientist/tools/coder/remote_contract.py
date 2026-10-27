"""Typed OpenHands task contract; execution receipts never establish scientific truth."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from coscientist.core.events.digest import canonical_json, sha256_hex
from coscientist.tools.contract import ToolError, ToolErrorKind


class RemoteCoderError(ToolError):
    def __init__(self, reason: str, *, kind: ToolErrorKind = "local") -> None:
        self.reason = reason
        super().__init__(kind, reason, retryable=False)


def bare(value: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or any(ord(c) < 32 for c in value):
        raise RemoteCoderError("remote_identity_invalid")
    return value


def workspace_path(value: str) -> str:
    bare(value)
    path = PurePosixPath(value)
    if path.is_absolute() or "\\" in value or str(path) != value or any(part in (".", "..") for part in path.parts):
        raise RemoteCoderError("remote_workspace_path_invalid")
    return value


def digest(value: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise RemoteCoderError("remote_digest_invalid")
    return value


def endpoint(value: str) -> str:
    bare(value)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RemoteCoderError("remote_endpoint_invalid") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or port == 0):
        raise RemoteCoderError("remote_endpoint_invalid")
    return value.rstrip("/")


@dataclass(frozen=True)
class RemoteInput:
    artifact_id: str
    sha256: str
    size: int
    path: str
    url: str
    required: bool

    def __post_init__(self) -> None:
        bare(self.artifact_id)
        digest(self.sha256)
        workspace_path(self.path)
        if type(self.size) is not int or self.size < 0 or type(self.required) is not bool:
            raise RemoteCoderError("remote_input_invalid")
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise RemoteCoderError("remote_input_url_invalid")

    def identity(self) -> dict[str, object]:
        return {"artifact_id": self.artifact_id, "sha256": self.sha256, "size": self.size,
                "path": self.path, "required": self.required}

    def wire(self) -> dict[str, object]:
        return {"url": self.url, "path": self.path, "sha256": self.sha256, "size": self.size,
                "required": self.required}


@dataclass(frozen=True)
class RemoteOutput:
    path: str
    media_type: str
    sha256: str | None
    size: int | None

    def __post_init__(self) -> None:
        workspace_path(self.path)
        bare(self.media_type)
        if self.sha256 is not None:
            digest(self.sha256)
        if self.size is not None and (type(self.size) is not int or self.size < 0):
            raise RemoteCoderError("remote_output_size_invalid")


@dataclass(frozen=True)
class RemoteCoderTaskRequest:
    study_id: str
    attempt_id: str
    call_id: str
    task: str
    inputs: tuple[RemoteInput, ...]
    outputs: tuple[RemoteOutput, ...]
    deadline: datetime
    wait_in_queue: bool

    def __post_init__(self) -> None:
        for value in (self.study_id, self.attempt_id, self.call_id):
            bare(value)
        if not isinstance(self.task, str) or not self.task.strip() or type(self.wait_in_queue) is not bool:
            raise RemoteCoderError("remote_task_invalid")
        if not isinstance(self.deadline, datetime) or self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise RemoteCoderError("remote_deadline_naive")
        paths = [entry.path for entry in self.inputs] + [entry.path for entry in self.outputs]
        if len(paths) != len(set(paths)):
            raise RemoteCoderError("remote_duplicate_path")
        if not self.outputs:
            raise RemoteCoderError("remote_outputs_required")

    def identity(self, service: str, policy_digest: str) -> dict[str, object]:
        return {"study_id": self.study_id, "attempt_id": self.attempt_id, "call_id": self.call_id,
                "endpoint": endpoint(service), "policy_digest": digest(policy_digest), "task": self.task,
                "inputs": [entry.identity() for entry in self.inputs],
                "outputs": [{"path": entry.path, "media_type": entry.media_type,
                             "sha256": entry.sha256, "size": entry.size} for entry in self.outputs],
                "deadline": self.deadline.isoformat(), "wait": self.wait_in_queue, "new_container": True}

    def wire(self) -> bytes:
        return canonical_json({"task": self.task, "wait": self.wait_in_queue, "new_container": True,
                               "inputs": [entry.wire() for entry in self.inputs]})


RemoteStatus = Literal["queued", "running", "soft_stopping", "cooldown", "completed", "error", "cancelled"]


class SubmitIdentity(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", frozen=True)
    task_id: str = Field(min_length=1, max_length=256)


class TaskIdentity(SubmitIdentity):
    status: RemoteStatus


@dataclass(frozen=True)
class HttpReceipt:
    operation: str
    status_code: int
    body: bytes

    @property
    def sha256(self) -> str:
        return sha256_hex(self.body)
