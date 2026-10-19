from __future__ import annotations

from pydantic import BaseModel, Field

# These mirror the JSON contract served by the Next.js worker API
# (/api/worker/claim | complete | fail | heartbeat). Keep field names in sync
# with frontend/src/app/api/worker/*.


class ResultTarget(BaseModel):
    # Presigned S3 PUT URL the node uploads the result to, and the object key
    # the API records on complete.
    put_url: str
    key: str
    content_type: str = "image/jpeg"


class ClaimedJob(BaseModel):
    id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    lease_token: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    attempt: int = Field(ge=1)
    task_type: str
    params: dict = Field(default_factory=dict)
    # name -> presigned S3 GET URL (e.g. {"target": "...", "source": "..."}).
    inputs: dict[str, str] = Field(default_factory=dict)
    result: ResultTarget
    # How long the API leases the job to this node before a reaper may requeue
    # it; the node heartbeats within this window.
    lease_seconds: int = Field(default=120, ge=1)


class ClaimResponse(BaseModel):
    # `job` is null when the queue has nothing for this node's tier/capabilities.
    job: ClaimedJob | None = None
