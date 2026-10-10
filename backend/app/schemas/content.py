from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import Input


class ToolOut(BaseModel):
    id: str
    code: str
    name: str
    description: str
    route: str
    enabled: bool
    sort_order: int
    icon_url: str | None = None


class FeedbackInput(Input):
    category: Literal["invoice", "tax", "export", "account", "other"]
    content: str = Field(min_length=1, max_length=5000)
    attachment_file_ids: list[str] = Field(default_factory=list)


class FeedbackOut(BaseModel):
    id: str
    status: str
    created_at: str
