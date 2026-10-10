from pydantic import BaseModel, Field

from app.schemas.common import Input


class WechatLogin(Input):
    code: str = Field(min_length=1, max_length=256)


class DevLogin(Input):
    account: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")


class RefreshInput(Input):
    refresh_token: str = Field(min_length=32, max_length=256)


class UserOut(BaseModel):
    id: str
    display_name: str
    avatar_url: str | None
    version: int
    created_at: str


class AuthResult(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    user: UserOut


class UserPatch(Input):
    display_name: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)


class NotificationPatch(Input):
    in_app_task_updates: bool
    expected_version: int = Field(ge=1)


class PrivacyPatch(Input):
    retain_chat_history: bool
    expected_version: int = Field(ge=1)


class NotificationOut(BaseModel):
    version: int
    in_app_task_updates: bool
    wechat_subscription_available: bool = False


class PrivacyOut(BaseModel):
    version: int
    retain_chat_history: bool
    applies_to: str = "future_sessions"
    retention_policy: dict[str, int | None]


class SecurityOut(BaseModel):
    account_status: str
    wechat_bound: bool
    last_login_at: str
    account_deletion_available: bool = False
    support_contact: str
