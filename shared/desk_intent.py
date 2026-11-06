"""The only shape a desk command may take (S-8). There is no free-text `command` or `args` field and extras are
forbidden, so a body carrying one fails validation (422 in FastAPI). Verb membership and tier are NOT checked here:
the route maps unknown verb -> 422, T2 -> 403, T3 -> 403 itself (see shared.desk_allowlist).
`actor` is asserted by the key holder, not authenticated. Never log request bodies.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from shared.desk_allowlist import check_verb, is_valid_id, missing_required


class CommandIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["command"]
    verb: str
    project: str
    sprint: str
    story_id: str | None = None
    plan_digest: str | None = None
    evidence: str | None = None
    actor: str

    @field_validator("project", "sprint", "story_id", "actor")
    @classmethod
    def _id_shaped(cls, value):
        if value is not None and not is_valid_id(value):
            raise ValueError("must match the id pattern")  # no echo of the value
        return value


def intent_verdict(intent: CommandIntent) -> tuple[str, str]:
    """check_verb for this intent; raises ValueError on an unknown verb."""
    return check_verb(intent.verb)


def intent_missing(intent: CommandIntent) -> list[str]:
    """Required fields the intent's verb needs that are missing or blank."""
    return missing_required(intent.verb, intent.model_dump())
