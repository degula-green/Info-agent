from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    READY = "ready"
    METADATA_ONLY = "metadata_only"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ParseStatus(StrEnum):
    PENDING = "pending"
    PARSED = "parsed"
    METADATA_ONLY = "metadata_only"
    FAILED = "failed"


class ProjectionStatus(StrEnum):
    PENDING = "pending"
    INDEXING = "indexing"
    READY = "ready"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    DELETED = "deleted"


JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset(
        {JobStatus.PROCESSING, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.PROCESSING: frozenset(
        {
            JobStatus.PROCESSING,
            JobStatus.RETRY_WAIT,
            JobStatus.READY,
            JobStatus.METADATA_ONLY,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.RETRY_WAIT: frozenset(
        {JobStatus.PROCESSING, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.READY: frozenset({JobStatus.READY}),
    JobStatus.METADATA_ONLY: frozenset({JobStatus.METADATA_ONLY}),
    JobStatus.FAILED: frozenset({JobStatus.FAILED}),
    JobStatus.CANCELLED: frozenset({JobStatus.CANCELLED}),
}

PROJECTION_TRANSITIONS: dict[ProjectionStatus, frozenset[ProjectionStatus]] = {
    ProjectionStatus.PENDING: frozenset(
        {ProjectionStatus.INDEXING, ProjectionStatus.RETRY_WAIT, ProjectionStatus.FAILED}
    ),
    ProjectionStatus.INDEXING: frozenset(
        {ProjectionStatus.READY, ProjectionStatus.RETRY_WAIT, ProjectionStatus.FAILED}
    ),
    ProjectionStatus.RETRY_WAIT: frozenset(
        {ProjectionStatus.INDEXING, ProjectionStatus.FAILED}
    ),
    ProjectionStatus.READY: frozenset(
        {ProjectionStatus.READY, ProjectionStatus.DELETED}
    ),
    ProjectionStatus.FAILED: frozenset({ProjectionStatus.FAILED}),
    ProjectionStatus.DELETED: frozenset({ProjectionStatus.DELETED}),
}

EXTERNAL_JOB_STATUS: dict[JobStatus, str] = {
    JobStatus.PENDING: "processing",
    JobStatus.PROCESSING: "processing",
    JobStatus.RETRY_WAIT: "processing",
    JobStatus.READY: "ready",
    JobStatus.METADATA_ONLY: "metadata_only",
    JobStatus.FAILED: "failed",
    JobStatus.CANCELLED: "failed",
}


def validate_job_transition(current: str, target: str) -> None:
    try:
        source = JobStatus(current)
        destination = JobStatus(target)
    except ValueError as exc:
        raise ValueError(f"unsupported job transition: {current} -> {target}") from exc
    if destination not in JOB_TRANSITIONS[source]:
        raise ValueError(f"illegal job transition: {current} -> {target}")


def validate_projection_transition(current: str, target: str) -> None:
    try:
        source = ProjectionStatus(current)
        destination = ProjectionStatus(target)
    except ValueError as exc:
        raise ValueError(
            f"unsupported projection transition: {current} -> {target}"
        ) from exc
    if destination not in PROJECTION_TRANSITIONS[source]:
        raise ValueError(f"illegal projection transition: {current} -> {target}")
