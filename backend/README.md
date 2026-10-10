# 金融工具箱后端

当前版本 0.4.0，完成基础工程、票据、导出/分享及 LangGraph 聊天会话、澄清确认与持久化编排。需求与目标契约位于项目 docs，实际已实现接口见 [开发进度](../docs/development-status.md) 和 [OpenAPI](../docs/openapi.json)。

## 环境

Python 3.12、uv。本地使用 SQLite 和私有本地文件目录；数据库模型与迁移兼容 PostgreSQL。已接入 COS 和腾讯 OCR 适配，聊天模型已有可配置工具调用 HTTP 适配，默认关闭；生产环境启动校验会拒绝开发登录、SQLite 和本地存储。

## 启动

在本目录执行，示例为 PowerShell：

```powershell
uv sync --locked --extra dev
uv run --no-sync python -m scripts.init_local
uv run --no-sync alembic upgrade head
uv run --no-sync uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

init_local 只在 .env 不存在时生成随机密钥，不覆盖已有配置，不显示密钥。不要将 .env、var 或虚拟环境提交到版本库。API 启动不自动创建表，首次启动先执行迁移。

另开终端运行 Worker：

```powershell
uv run --no-sync python -m app.jobs.worker
```

执行一个待处理任务后退出：

```powershell
uv run --no-sync python -m app.jobs.worker --once
```

接口文档 `/docs`，机器接口规范 `/openapi.json`，存活检查 `/health/live`，数据库迁移就绪检查 `/health/ready`。访问本机服务使用 http://127.0.0.1:8000。

## 登录配置

真实微信登录配置 `.env` 中 FT_WECHAT_APP_ID 和 FT_WECHAT_APP_SECRET，使用 `/api/v1/auth/wechat-login`，提交微信实时 code。真实微信调用尚未用生产凭证验证；不要将临时 code、AppSecret 或 token 放入日志或文档。

开发联调可在本地 .env 显式添加 `FT_ENABLE_DEV_LOGIN=true` 后重启 API，调用：

```json
{"account":"frontend-dev"}
```

路径 `/api/v1/auth/dev-login`，仅非生产且显式启用才可调用。它不模拟微信临时 code 的认证过程。返回的 access_token 用于 Bearer 鉴权；refresh_token 每次使用后轮换；登出立即撤销该会话已有 access token。

开发端口不要暴露到公网。运行命令使用 --no-access-log，避免签名上传/下载地址中的凭证出现在默认访问日志。

## 文件流程

1. 创建 upload-session，金额以外的字节数同样为字符串。
2. 请求 part-urls，按服务端 part_size_bytes 上传每片，保留原样 ETag（包含双引号）。
3. complete 返回 file_id 与 validating；Worker 合并、校验哈希并验证真实格式。
4. 文件变为 ready 后才可授权下载；损坏/加密文件为 rejected。

本地签名地址是开发用传输实现，只支持单个 API 进程，suggested_concurrency=1；不使用多个 Uvicorn workers。COS 模式使用已实现的直传适配器，真实云资源和微信真机联调尚未进行。文件处理流式合并，不设产品总大小和 PDF 页数上限；仍有文件系统及客户端的真实边界。

签名 URL 是短期访问凭证，不随意转发。取消上传会话阻止新上传，不承诺删除已完成文件；自动过期文件清理和保留策略仍待实施。

## 发票识别配置和联调

云服务凭证配置、票种验证状态与前端步骤见 [发票与云服务对接指南](../docs/invoice-integration.md)。OCR 默认 disabled；设置 FT_OCR_PROVIDER=tencent 并配置密钥后重启 API 与 Worker。无需切换 COS 即可使用本地文件接 OCR。

原始票据与核对结果分别保存；succeeded 表示处理完成，pending_review/confirmed 表示核对状态。供应商识别未用真实凭证与样本验证。

## 导出与分享

[导出与分享指南](../docs/export-integration.md) 提供实际接口流程、文件内容和访问期限。默认本地环境即可生成文件，须同时运行 Worker。FT_EXPORT_RETENTION_DAYS 默认 7 天；FT_PDF_FONT_PATH 可配置有授权的中文 TTF，否则使用标准中文 CID 字体。

导出数据创建时保存为快照；分享只能主动创建，普通查询不返回 token。访问过期与物理删除不同，自动数据清理仍待实施。

## 聊天助手

实现 LangGraph Python 编排、40 个工具、持久化会话/run/action/事件和任务恢复。配置、前端流程、确认与取消规则见 [聊天对接指南](../docs/chat-integration.md)。新增模型配置必须同时用于 API 和 Worker；默认 FT_MODEL_PROVIDER=disabled，需要模型名称、基础地址和服务端 API 密钥才能启用。真实供应商尚未验证。

图检查点与业务效果在同一数据库事务提交。取消 run 默认不取消其已创建业务任务。关闭历史仅作用于新会话，临时会话 24 小时到期后由 Worker 清理；会话删除不会删除票据、拆税、导出和分享。分享凭证加密保存，不发给模型或放入普通事件。

本机验证可运行 `uv run --no-sync python -m scripts.smoke_stage4`，会启动真实本机 API、独立 Worker 和本地模型 HTTP 模拟接口，检查重启恢复、拆税/导出/分享及事件。它不使用真实模型或云凭证，不会自动开启当前 .env 的模型。

## PostgreSQL

配置 FT_DATABASE_URL 为 `postgresql+psycopg://用户:密码@地址:5432/数据库`，执行同一迁移。URL 中特殊字符需编码。SQLite 是开发替代环境，金额使用精确字符串存储；PostgreSQL 使用 NUMERIC。

本轮只验证 SQLite 集成与 PostgreSQL 离线迁移 SQL，尚未进行真实 PostgreSQL 并发/恢复测试。environment=production 要求 PostgreSQL、COS 及关闭开发登录。配置校验通过不表示完成上线验证。

## 验证和文档更新

```powershell
uv run --no-sync pytest -q
uv run --no-sync ruff check app tests scripts migrations
uv run --no-sync alembic check
uv run --no-sync python -m scripts.export_openapi
uv run --no-sync python -m scripts.smoke_stage3
uv run --no-sync python -m scripts.smoke_stage4
```

openapi 导出仅覆盖已实现接口，包含当前发票接口，包含导出分享，包含真实聊天持久化接口；未配置模型时发消息明确返回能力未开启。模型/OCR 未配置时能力开关为 false；所有实际业务创建、状态和文件校验结果均来自数据库与真实文件。

## 下一阶段

下一阶段进行真实模型业务评测、云服务与 PostgreSQL 并发联调，并完善用户级限流、保留清理和部署检查。
