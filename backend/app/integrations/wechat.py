from typing import Protocol

import httpx

from app.core.config import Settings
from app.core.errors import BusinessError


class WechatGateway(Protocol):
    def exchange(self, code: str) -> str: ...


class HttpWechatGateway:
    def __init__(self, settings: Settings):
        self.settings = settings

    def exchange(self, code: str) -> str:
        if (
            not self.settings.wechat_app_id
            or not self.settings.wechat_app_secret.get_secret_value()
        ):
            raise BusinessError("CAPABILITY_DISABLED", "尚未配置微信登录", 403)
        try:
            with httpx.Client(timeout=10) as client:
                result = client.get(
                    "https://api.weixin.qq.com/sns/jscode2session",
                    params={
                        "appid": self.settings.wechat_app_id,
                        "secret": self.settings.wechat_app_secret.get_secret_value(),
                        "js_code": code,
                        "grant_type": "authorization_code",
                    },
                )
                result.raise_for_status()
                payload = result.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BusinessError(
                "PROVIDER_UNAVAILABLE", "微信登录服务暂不可用", 503, retryable=True
            ) from exc
        if payload.get("errcode") == -1:
            raise BusinessError("PROVIDER_UNAVAILABLE", "微信登录服务暂忙", 503, retryable=True)
        if payload.get("errcode") or not payload.get("openid"):
            raise BusinessError("WECHAT_CODE_INVALID", "微信登录凭证无效，请重新登录", 401)
        return payload["openid"]
