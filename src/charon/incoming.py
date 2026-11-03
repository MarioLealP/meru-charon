"""The base for what Charon reads from outside (Gitea, the cluster): pydantic models, immutable and strictly typed, only
the fields it uses - the rest of an answer is ignored. A missing or mistyped field fails right there."""

from pydantic import BaseModel, ConfigDict


class Incoming(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")
