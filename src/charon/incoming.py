"""The base for what Charon reads from outside (Gitea, the cluster).

Pydantic models, immutable and strictly typed, only the fields it uses - the rest of an answer is ignored. A missing
or mistyped field fails right there.
"""

from pydantic import BaseModel, ConfigDict


class Incoming(BaseModel):
    """An answer from outside, validated on the way in."""

    model_config = ConfigDict(frozen=True, strict=True, extra="ignore")
