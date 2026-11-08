"""Small framework-neutral transport DTOs shared by QQ application adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

GroupPolicy = Literal["mention", "open"] | Annotated[float, Field(ge=0, le=1)]


class QQTransportConfig(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    enabled: bool = False
    ws_url: str = "ws://127.0.0.1:3001"
    access_token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    group_policy: GroupPolicy = "mention"
    group_policy_overrides: dict[str, GroupPolicy] = Field(default_factory=dict)
    welcome_new_members: bool = True
    max_image_bytes: int = Field(default=20 * 1024 * 1024, ge=1)


@dataclass
class OutboundMessage:
    channel: str
    chat_id: str
    content: str
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
