from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FT_", env_file=".env", extra="ignore")
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./var/toolbox.db"
    auth_signing_key: SecretStr
    public_base_url: str = "http://127.0.0.1:8000"
    storage_backend: Literal["local", "cos"] = "local"
    storage_root: Path = Path("var/storage")
    cos_bucket: str = ""
    cos_region: str = "ap-guangzhou"
    cos_secret_id: SecretStr = SecretStr("")
    cos_secret_key: SecretStr = SecretStr("")
    cos_prefix: str = "financial-toolbox/"
    ocr_provider: Literal["disabled", "tencent"] = "disabled"
    ocr_secret_id: SecretStr = SecretStr("")
    ocr_secret_key: SecretStr = SecretStr("")
    ocr_region: str = "ap-guangzhou"
    ocr_timeout_seconds: int = Field(default=60, ge=5, le=300)
    model_provider: Literal["disabled", "chat_completions"] = "disabled"
    model_base_url: str = ""
    model_name: str = ""
    model_api_key: SecretStr = SecretStr("")
    model_timeout_seconds: int = Field(default=60, ge=5, le=180)
    model_max_output_tokens: int = Field(default=2048, ge=128, le=16384)
    agent_max_model_calls: int = Field(default=12, ge=1, le=100)
    agent_max_tool_calls: int = Field(default=30, ge=1, le=200)
    agent_max_total_tokens: int = Field(default=100000, ge=1000, le=1000000)
    agent_max_context_chars: int = Field(default=100000, ge=10000, le=1000000)
    agent_run_seconds: int = Field(default=86400, ge=60, le=604800)
    chat_action_seconds: int = Field(default=600, ge=30, le=3600)
    export_retention_days: int = Field(default=7, ge=1, le=3650)
    pdf_font_path: Path | None = None
    enable_dev_login: bool = False
    wechat_app_id: str = ""
    wechat_app_secret: SecretStr = SecretStr("")
    access_token_seconds: int = Field(default=900, ge=60)
    refresh_token_seconds: int = Field(default=2592000, ge=300)
    upload_session_seconds: int = Field(default=86400, ge=60)
    signed_url_seconds: int = Field(default=300, ge=30, le=3600)
    upload_part_bytes: int = Field(default=8388608, ge=1024)
    job_lease_seconds: int = Field(default=120, ge=30)

    @model_validator(mode="after")
    def validate_deployment(self):
        key = self.auth_signing_key.get_secret_value()
        if len(key) < 32 or key.startswith("replace-"):
            raise ValueError("FT_AUTH_SIGNING_KEY 必须配置至少 32 字符的随机密钥")
        if self.storage_backend == "cos" and not (
            self.cos_bucket
            and self.cos_region
            and self.cos_secret_id.get_secret_value()
            and self.cos_secret_key.get_secret_value()
        ):
            raise ValueError("COS 需要 bucket、region 和服务端密钥")
        if (
            ".." in self.cos_prefix.split("/")
            or "\\" in self.cos_prefix
            or not self.cos_prefix.strip("/")
        ):
            raise ValueError("COS 对象前缀无效")
        if self.ocr_provider == "tencent" and not (
            self.ocr_secret_id.get_secret_value() and self.ocr_secret_key.get_secret_value()
        ):
            raise ValueError("票据 OCR 需要配置服务端密钥")
        if self.model_provider != "disabled":
            from urllib.parse import urlsplit

            url = urlsplit(self.model_base_url)
            if not self.model_name or not self.model_api_key.get_secret_value():
                raise ValueError("聊天模型需要模型名称和服务端 API 密钥")
            if (
                url.scheme not in {"https", "http"}
                or not url.hostname
                or url.query
                or url.fragment
                or url.username
            ):
                raise ValueError("模型地址必须是服务端配置的 HTTP(S) 基础地址")
            if self.environment == "production" and url.scheme != "https":
                raise ValueError("生产模型接口需要 HTTPS")
        if self.environment == "production":
            if self.enable_dev_login:
                raise ValueError("生产环境禁止开发登录")
            if self.database_url.startswith("sqlite"):
                raise ValueError("生产环境需要 PostgreSQL")
            if self.storage_backend == "local":
                raise ValueError("本地文件存储仅供开发，生产请选择 COS 对象存储")
        return self
