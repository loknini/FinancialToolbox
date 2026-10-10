# 金融工具箱前后端接口对接文档

版本：V1.1（实现进度 0.4.0）  
更新日期：2026-10-09  
设计说明：[后端设计文档](backend-design.md)

本契约用于前端页面、聊天界面与 FastAPI 后端联调，覆盖原始开发文档的接口，并补充登录、上传、导出创建、识别核对及助手执行。本文为完整目标契约；当前已实现范围见 [开发进度](development-status.md)，本轮可调用接口见 [OpenAPI](openapi.json)。舍入方式、分享期限及保留策略仍按建议默认值推进。

## 1 通用约定

### 1.1 地址和身份

- 基础地址由环境配置提供；所有业务路径以 `/api/v1` 开头。
- 表中路径省略 `/api/v1`。标为“公开”的接口无须登录，其余发送 `Authorization: Bearer <access_token>`。
- 登录 code 来自微信，不接受前端自报 user_id/openid。用户归属由后端从登录态确定。
- ID 使用字符串，示例 batch_001 等为占位符。资源不存在或不属于本人均返回 404。
- JSON 请求 `Content-Type: application/json`，UTF-8；对象存储直传按返回 upload headers 执行，不加业务 Bearer。
- 客户端可发送 `X-Request-ID`，后端验证后使用或重新生成，并在响应头和 JSON 中返回。

### 1.2 响应封装

成功（含 201/202）统一：

```json
{
  "code": "OK",
  "message": "success",
  "request_id": "req_001",
  "data": {"id": "batch_001"}
}
```

错误：

```json
{
  "code": "VALIDATION_ERROR",
  "message": "请输入有效税率",
  "request_id": "req_002",
  "data": null,
  "details": [{"field": "tax_rate", "reason": "not_allowed"}],
  "retryable": false
}
```

前端依据 code 和字段错误展示，不解析 message 判断业务逻辑。所有响应尽量 JSON；网关错误可能非 JSON，前端回退通用提示。GET 成功 200，普通创建 201，异步受理 202，更新/撤销/取消 200。DELETE 返回 JSON，避免混用空 204。

### 1.3 数据类型

| 类型 | 约定 |
| --- | --- |
| 金额 | 十进制字符串，已计算结果两位小数，如 113.00；未知 null |
| 税率 | 十进制比例字符串，如 0.13；未知/免税不强制填零 |
| 数量/单价 | 十进制字符串，精度按票面保留 |
| 字节数 | 十进制整数字符串，如 1048576，避免大数精度丢失 |
| 时间 | ISO 8601 UTC，如 2026-10-09T03:00:00Z |
| 日期 | YYYY-MM-DD；票面日期不做时区转换 |
| 未知 | null；不以零、空对象或不存在字段替代未知必备字段 |
| 枚举 | 英文 code；前端使用响应 label 展示中文 |

客户端未知枚举展示通用状态，未知可选字段忽略；后端拒绝未声明的可写字段。PATCH 未出现字段不改，null 仅用于声明可空字段。客户端无法写入所有权、状态、存储 key 等系统字段。

### 1.4 列表、日期和排序

所有列表采用 cursor 分页：`limit` 默认 20、最大 100，`cursor` 不透明；返回 `items/next_cursor/has_more`，不要求总数。主列表按 created_at 降序、id 降序，同页游标包含筛选摘要，不能改筛选继续用旧游标。

```json
{
  "code": "OK",
  "message": "success",
  "request_id": "req_003",
  "data": {"items": [], "next_cursor": null, "has_more": false}
}
```

`date_from/date_to` 是 Asia/Shanghai 的自然日，包含两端；后端转 UTC 半开区间。最近 7 天包含今天及前 6 个自然日。未传日期不筛选，from 大于 to 返回 422。资源列表的日期默认针对 created_at；票据另有 invoice_date_from/to 针对票面日期。

### 1.5 幂等和版本

表中标记 K 的接口要求 `Idempotency-Key`，一个用户的一次操作使用同一随机键。相同键和请求内容返回相同资源；同键不同内容返回 IDEMPOTENCY_CONFLICT。建议响应 `Idempotency-Expires-At`，初值保留 24 小时；服务端助手工具用稳定 run/step key，不依赖客户端期限防重。

PATCH 票据和设置发送 expected_version；版本冲突 409 后重新拉取，不自动覆盖。取消、撤销等具有天然幂等语义，重复提交返回当前状态。确认 action 只能消费一次，重复同一选择返回原结果，不重复执行。

## 2 状态和错误

### 2.1 状态枚举

| 资源 | 状态 |
| --- | --- |
| 上传会话 | uploading/completing/completed/failed/expired/cancelled |
| 文件 | validating/ready/rejected/expired/deleted |
| 批次 | queued/processing/succeeded/partial_failed/failed/cancelling/cancelled |
| 处理单元 | queued/processing/succeeded/failed/cancelled |
| 票据核对 | pending_review/confirmed |
| 导出任务 | queued/processing/succeeded/failed/cancelling/cancelled |
| 导出文件 | pending/available/expired/deleted |
| 助手 run | queued/running/waiting_input/waiting_confirmation/waiting_task/succeeded/failed/cancelling/cancelled |
| action | pending/consumed/expired/cancelled |

queued/processing 为未完成，succeeded 不等于人工核对。统计单元不是发票数量。重试批次可再次变为 queued；票据修正值不丢失。

### 2.2 错误码

| HTTP | code | 前端处理 |
| --- | --- | --- |
| 400 | INVALID_CURSOR | 重置筛选和分页 |
| 401 | UNAUTHORIZED/TOKEN_EXPIRED | 可刷新一次，失败后重新登录 |
| 401 | WECHAT_CODE_INVALID | 获取新的微信 code，不重试旧 code |
| 403 | CAPABILITY_DISABLED | 显示功能暂未启用 |
| 404 | RESOURCE_NOT_FOUND | 提示不存在或不可访问 |
| 409 | IDEMPOTENCY_CONFLICT/VERSION_CONFLICT | 换新操作键或重新拉取版本 |
| 409 | FILE_NOT_READY/STATE_CONFLICT | 查询当前状态，再决定操作 |
| 409 | ACTIVE_RUN_EXISTS | 显示已有 run，不另建重复任务 |
| 409 | ACTION_STALE | 服务端重新准备 action，前端展示更新后范围 |
| 409 | CONFIG_CHANGED | 刷新拆税配置并重新确认参数 |
| 409 | SHARE_ALREADY_EXISTS | 展示现有分享状态，需新链接时先撤销 |
| 410 | FILE_EXPIRED/UPLOAD_EXPIRED/SHARE_EXPIRED/ACTION_EXPIRED | 重新上传、生成、分享或准备 action |
| 410 | EVENT_CURSOR_EXPIRED | 获取会话快照并重置事件游标 |
| 422 | VALIDATION_ERROR/UNSUPPORTED_FORMAT/EMPTY_EXPORT/NO_FAILED_ITEMS | 展示字段或业务提示 |
| 422 | PASSWORD_PROTECTED_PDF/CORRUPTED_FILE | 请求用户提供可读文件 |
| 422 | REVIEW_REQUIRED | 解决票据中 severity=error 的问题后再确认 |
| 404 | SHARE_UNAVAILABLE | 分享无效或已撤销，联系分享者 |
| 413 | INFRASTRUCTURE_FILE_LIMIT | 技术边界，展示支持说明；不是产品硬编码限额 |
| 429 | RATE_LIMITED | 按 Retry-After 重试，不立即循环请求 |
| 502/503/504 | PROVIDER_UNAVAILABLE/SERVICE_UNAVAILABLE/UPSTREAM_TIMEOUT | 可重试，保持创建操作幂等键 |

异步任务失败仍以 GET 200 返回资源，资源含 error，不将失败任务查询转换成 HTTP 500。error 至少含 code/message/retryable；逐项失败包含 unit_id。供应商原始内部错误不直接暴露。

## 3 登录和用户

| 方法 | 路径 | 请求 | 返回 data / 状态 |
| --- | --- | --- | --- |
| POST 公开 | /auth/wechat-login | code 必填字符串 | AuthResult / 200 |
| POST 公开 | /auth/refresh | refresh_token | AuthResult / 200，旧 token 失效 |
| POST | /auth/logout | 空对象 | logged_out=true / 200 |
| GET | /users/me | 无 | User |
| PATCH | /users/me | display_name、expected_version | User |
| GET | /users/me/security | 无 | Security |
| POST K | /users/me/deletion-requests | acknowledgement=true | id/status=requested / 202；能力未启用 403 |
| GET | /users/me/notification-settings | 无 | NotificationSettings |
| PATCH | /users/me/notification-settings | in_app_task_updates、expected_version | NotificationSettings |
| GET | /users/me/privacy-settings | 无 | PrivacySettings |
| PATCH | /users/me/privacy-settings | retain_chat_history、expected_version | PrivacySettings |
| GET | /users/me/tools | cursor/limit | Tool 列表，按最近使用排序 |

AuthResult：access_token、token_type=Bearer、expires_in 秒、refresh_token、refresh_expires_in 秒、user。refresh 接口无 Bearer，但必须有有效 refresh token。

User：id、display_name、avatar_url 可空（首期只读）、version、created_at。display_name 长度 1 至 64，去除首尾空格。

Security：account_status、wechat_bound、last_login_at、account_deletion_available、support_contact，不返回 openid/session_key。

NotificationSettings：version、in_app_task_updates、wechat_subscription_available=false。未启用的微信通知开关不可写。

PrivacySettings：version、retain_chat_history、applies_to=future_sessions、retention_policy（chat_days/files_days/export_days 可 null 表示尚未配置，不表示永久保存）。开关不删除既有历史；删除历史走会话删除接口。生产必须配置实际政策。

## 4 工具和内容

| 方法 | 路径 | 参数 | 返回 |
| --- | --- | --- | --- |
| GET 公开 | /tools | keyword、cursor、limit | Tool 列表 |
| GET 公开 | /content/tips/invoice | 无 | items [{id,title,body}] |
| GET 公开 | /help/categories | 无 | items [{code,name}] |
| GET 公开 | /help/articles | category、keyword、cursor、limit | Article 列表 |
| GET 公开 | /help/articles/{slug} | slug，如 export-formats | Article |
| GET 公开 | /app/about | 无 | app_name/version/description/support_contact |
| GET 公开 | /capabilities | 无 | 上传、票种、助手及产品能力配置 |
| POST K | /feedback | category、content、attachment_file_ids | id/status=received/created_at / 201 |

Tool：id、code、name、description、route、enabled、sort_order、icon_url 可空。
Article：slug、category、title、body（纯文本或明确标记的 Markdown）、body_format、version、updated_at。反馈 content 长度 1 至 5000，category 必须来自帮助分类，附件为本人用途 feedback 且 ready 的文件。

能力响应示例（票种条目仅示例，实际由验证结果配置）：

```json
{
  "uploads": {
    "supported_formats": ["pdf", "jpeg", "png"],
    "product_max_size_bytes": null,
    "product_max_pdf_pages": null,
    "multipart": true,
    "technical_limits": {"max_part_count": 10000, "max_object_size_bytes": null},
    "unsupported_cases": ["password_protected_pdf"]
  },
  "invoice_types": [{"code": "vat_invoice", "label": "增值税发票", "recognition_enabled": true, "validation_status": "sample_verified"}],
  "chat": {"enabled": true, "event_transport": "http_long_poll"},
  "share_default_days": 7,
  "account_deletion_available": false,
  "wechat_subscription_available": false
}
```

技术 max_part_count 由存储实际能力返回，示例不构成产品页数限制。null 的技术 max_object_size 表示未给出能力值，应结合上传会话返回参数，不可据此承诺无限上传。

## 5 文件上传

### 5.1 路由

| 方法 | 路径 | 请求/参数 | 返回 |
| --- | --- | --- | --- |
| POST K | /upload-sessions | filename/content_type/size_bytes/purpose | UploadSession / 201 |
| GET | /upload-sessions/{id} | 无 | UploadSession，含已上传 parts |
| POST | /upload-sessions/{id}/part-urls | part_numbers 整数数组 | parts [{part_number,url,method,headers,expires_at}] |
| POST K | /upload-sessions/{id}/complete | parts [{part_number,etag}] | upload_session_id/file_id/status=validating / 202 |
| POST | /upload-sessions/{id}/renew | 空对象 | UploadSession；已过期 410 |
| DELETE | /upload-sessions/{id} | 无 | id/status=cancelled |
| GET | /files/{id} | 无 | File |
| GET | /files/{id}/download-url | 无 | DownloadUrl |

purpose 枚举 invoice/feedback/chat_attachment。当前识别只接受 invoice 或 chat_attachment 的 PDF/JPEG/PNG。文件不能因用途不同绕过格式与归属检查。

UploadSession：id、status、filename、size_bytes、purpose、part_size_bytes、max_part_count、suggested_concurrency、expires_at、parts、file_id 可空。服务端按预期大小和存储能力选择 part_size，不硬编码客户端分片大小。parts 是已上传部分的编号和 ETag。

File：id、filename、content_type、size_bytes、purpose、status、sha256 可空、error 可空、created_at、expires_at 可空。

DownloadUrl：url、expires_at、filename、content_type、size_bytes。过期重新请求，不把签名 URL 长期存为文件标识。

上传请求示例：

```json
{"filename":"本月发票.pdf","content_type":"application/pdf","size_bytes":"10485760","purpose":"invoice"}
```

完成请求示例：

```json
{"parts":[{"part_number":1,"etag":"etag_part_1"},{"part_number":2,"etag":"etag_part_2"}]}
```

### 5.2 前端步骤

1. 选择文件或拍照，创建上传会话，记录会话 ID。
2. 按 part_size_bytes 切片，分批申请签名地址；签名到期重新申请。
3. 使用响应 method/headers 上传每片，保存存储返回 ETag；不用业务鉴权头直传。
4. 完成上传；轮询 File 直到 ready 或 rejected。
5. ready 后才创建识别批次或发送关联附件消息。
6. 暂停后 GET 会话获取已完成部分，仅补传缺失部分。失效会话重新创建。

真机验证 uni-app/微信文件系统的切片能力及存储响应 ETag 获取；若客户端无法读取所需 ETag，改用后端查询分片结果，不要求前端猜测。页面上传进度与后台文件验证进度分别展示。

## 6 发票批次与票据

### 6.1 路由

| 方法 | 路径 | 请求/查询 | 返回 |
| --- | --- | --- | --- |
| POST K | /invoice-batches | file_ids、source=page/chat | Batch / 202 |
| GET | /invoice-batches | status、date_from/to、cursor/limit | Batch 列表 |
| GET | /invoice-batches/{id} | 无 | Batch |
| GET | /invoice-batches/{id}/items | status、cursor/limit | ProcessingUnit 列表 |
| POST K | /invoice-batches/{id}/retry | scope=failed | Batch / 202 |
| POST | /invoice-batches/{id}/cancel | 空对象 | Batch / 200 |
| GET | /invoices | batch_id/type_code/review_status/keyword/date_from/to/invoice_date_from/to/cursor/limit | InvoiceSummary 列表 |
| GET | /invoices/{id} | 无 | InvoiceDetail |
| PATCH | /invoices/{id} | InvoicePatch | InvoiceDetail |
| POST | /invoices/{id}/confirm | expected_version | InvoiceDetail |

file_ids 为非空、不重复数组，均属本人且 ready；已在活动任务中的文件可建立新的独立批次，但幂等键防止误操作重复。重试无失败单元返回 NO_FAILED_ITEMS，批次尚在运行返回 STATE_CONFLICT。

### 6.2 Batch

```json
{
  "id":"batch_001",
  "status":"processing",
  "source":"page",
  "file_count":1,
  "discovery_complete":true,
  "progress":{"total_units":12,"completed_units":7,"succeeded_units":6,"failed_units":1,"cancelled_units":0,"percent":58},
  "invoice_count":8,
  "pending_review_count":3,
  "can_retry":false,
  "can_cancel":true,
  "error":null,
  "created_at":"2026-10-09T03:00:00Z",
  "updated_at":"2026-10-09T03:01:00Z"
}
```

total_units 在发现完成前为 null，percent 也为 null，此时显示“正在分析文件”。发现完成后 percent=floor(completed_units/total_units*100)，completed 包含成功/失败/取消。终态显示具体结果，不能以 100% 代替成功状态。没有发现单元的损坏文件直接 failed，total_units=0、percent=null。

ProcessingUnit：id、file_id、page_from/page_to（从 1 开始）、status、attempt、invoice_count、error。列表按文件顺序和页码排序。

### 6.3 票据结构

InvoiceSummary：id、batch_id、type_code/type_label、invoice_number 可空、invoice_date 可空、seller_name 可空、gross_amount/net_amount/tax_amount 可空、currency、review_status、version、issues、created_at。

InvoiceDetail 示例：

```json
{
  "id":"inv_001",
  "batch_id":"batch_001",
  "type_code":"vat_invoice",
  "type_label":"增值税发票",
  "invoice_number":"EXAMPLE001",
  "invoice_code":null,
  "invoice_date":"2026-10-08",
  "buyer":{"name":"示例购买方","tax_id":null},
  "seller":{"name":"示例销售方","tax_id":null},
  "gross_amount":"113.00",
  "net_amount":"100.00",
  "tax_amount":"13.00",
  "currency":"CNY",
  "lines":[{"line_number":1,"name":"示例商品","quantity":"1","unit_price":"100.00","net_amount":"100.00","tax_rate":"0.13","tax_treatment":"taxable","tax_amount":"13.00"}],
  "source":{"file_id":"file_001","page_from":1,"page_to":1,"region":null},
  "review_status":"pending_review",
  "issues":[],
  "version":1,
  "extended_fields":{},
  "created_at":"2026-10-09T03:00:00Z"
}
```

tax_treatment 为 taxable/exempt/non_taxable/zero_rated/unknown，免税税率可 null。extended_fields 是只读供应商/票种扩展字段。issues 为 [{field,code,message,severity}]，severity=warning/error。详情中的 lines 可为空，不把缺少明细视为零明细金额。

InvoicePatch：expected_version 必填、reason 可选；可写 invoice_number/code/date、buyer/seller 的 name/tax_id、gross/net/tax_amount、lines。lines 出现时整体替换，不按数组位置部分合并；金额可 null，金额修改重新运行一致性校验并将 review_status 设 pending_review。currency 只读，常规国内票据默认为 CNY；取供应商明确币种，海外币种未知时为 UNKNOWN，无汇率换算。票种修正先由后端适配，不开放任意 type_code。

```json
{"expected_version":1,"seller":{"name":"修正后的销售方"},"reason":"核对原文件"}
```

confirm 基于当前版本，若存在 severity=error 则拒绝确认并返回 REVIEW_REQUIRED（422）；warning 可在用户明确确认后通过，审计记录保留。修改后 version 增加，前端刷新卡片。列表只返回摘要，明细按需拉取。

## 7 拆税

| 方法 | 路径 | 请求/查询 | 返回 |
| --- | --- | --- | --- |
| GET 公开 | /tax-calculations/config | 无 | TaxConfig |
| POST K | /tax-calculations | gross_amount/tax_rate/invoice_type_code/config_version/source_invoice_id 可选 | TaxCalculation / 201 |
| GET | /tax-calculations | date_from/to、cursor/limit | TaxCalculation 列表 |
| GET | /tax-calculations/{id} | 无 | TaxCalculation |

TaxConfig：version、rule_version、input_mode=gross、amount_scale=2、rounding=half_up、rates [{value,label}]、invoice_types [{code,label,allowed_rates}]。发票类型表示手工工具选项，不直接由票种名称推断法定税率。版本过期返回 CONFIG_CHANGED（409），前端刷新配置再让用户确认。

示例 data：

```json
{
  "version":"tax-config-v1",
  "rule_version":"gross-split-v1",
  "input_mode":"gross",
  "amount_scale":2,
  "rounding":"half_up",
  "rates":[{"value":"0.01","label":"1%"},{"value":"0.03","label":"3%"},{"value":"0.06","label":"6%"},{"value":"0.09","label":"9%"},{"value":"0.13","label":"13%"}],
  "invoice_types":[{"code":"general","label":"通用拆税","allowed_rates":["0.01","0.03","0.06","0.09","0.13"]}]
}
```

请求：

```json
{"gross_amount":"113.00","tax_rate":"0.13","invoice_type_code":"general","config_version":"tax-config-v1"}
```

响应 data：

```json
{"id":"tax_001","gross_amount":"113.00","net_amount":"100.00","tax_amount":"13.00","tax_rate":"0.13","invoice_type_code":"general","rule_version":"gross-split-v1","config_version":"tax-config-v1","source_invoice_id":null,"created_at":"2026-10-09T03:00:00Z"}
```

gross_amount 正数、最多两位小数；税率在配置范围内。关联发票只作为来源，不改票面。前端预览不能替代后端最终结果，重复提交同键只保存一次。

## 8 导出、下载、分享

### 8.1 路由

| 方法 | 路径 | 请求/查询 | 返回 |
| --- | --- | --- | --- |
| POST K | /exports | ExportRequest | Export / 202 |
| GET | /exports | format=excel/pdf、status、date_from/to、cursor/limit | Export 列表 |
| GET | /exports/{id} | 无 | Export |
| POST | /exports/{id}/cancel | 空对象 | Export |
| GET | /exports/{id}/download-url | 无 | DownloadUrl |
| POST K | /exports/{id}/share | expires_in_days 默认 7 | Share / 201 |
| GET | /exports/{id}/share | 无 | 当前 Share 可 null，不返回 token |
| DELETE | /exports/{id}/share | 无 | revoked=true |
| GET 公开 | /shares/{token} | 无 | ShareView |
| GET 公开 | /shares/{token}/download-url | 无 | DownloadUrl |

首期每个导出最多一个活动分享；重复创建返回已有状态但不明文回传旧 token，需显示 SHARE_ALREADY_EXISTS（409），撤销后可新建。幂等重放可在安全响应保留期内返回同一创建结果。首次创建返回完整 token/路径，由前端保存分享结果；普通查询只给状态和到期时间。

### 8.2 导出请求和来源

format=excel/pdf，source_type=invoices/tax_calculations，selection 恰好选一种 mode：ids/batch/filter。

- ids：ids 非空数组，均属当前用户。
- batch：batch_id，只允许 source_type=invoices。
- filter：date_from/to 可选，票据还允许 type_code、invoice_date_from/to；不接受任意数据库字段。
- invoices 的 review_scope=confirmed/all，省略或 null 时默认 confirmed；tax_calculations 不传 review_scope。
- group_by 默认 none；票据允许 seller/month，计算允许 month，均影响呈现，不改变原始金额。

```json
{"format":"excel","source_type":"invoices","selection":{"mode":"batch","batch_id":"batch_001"},"review_scope":"all","group_by":"seller"}
```

filter 请求默认针对创建时间，票面日期筛选使用 invoice_date 字段。“本月发票”存在语义歧义时助手询问按上传日期还是票面日期筛选。

### 8.3 导出响应

```json
{
  "id":"exp_001",
  "format":"excel",
  "source_type":"invoices",
  "status":"succeeded",
  "record_count":8,
  "pending_review_count":3,
  "snapshot_at":"2026-10-09T03:02:00Z",
  "progress":{"percent":100},
  "file":{"id":"file_export_001","status":"available","filename":"发票汇总.xlsx","size_bytes":"28416","expires_at":"2026-10-16T03:02:00Z"},
  "error":null,
  "can_cancel":false,
  "created_at":"2026-10-09T03:02:00Z"
}
```

file 在生成前可 null，record_count 在快照完成前可 null；progress.percent 可 null。expires_at 示例不是已确定的导出保留政策。任务成功但文件 expired 时显示“文件已过期”，不能仍显示可下载。

快照不随来源后续修改而变化。未匹配数据返回 EMPTY_EXPORT。批次运行中仍可显式导出已完成且符合核对条件的记录，但前端必须展示“仅包含快照时已识别的数据”。

分享创建：

```json
{"expires_in_days":7}
```

Share：id/export_id/status=active/revoked/expired、expires_at、created_at；首次创建额外 token、share_path（例如 /pages/share/index?token=...）。期限允许 1 至 30 天，默认 7 为建议值。仅 available 文件可分享。

ShareView：filename、format、size_bytes、expires_at、description，不含用户身份、其他票据或原文件地址。ShareView 中 expires_at 是分享期限；下载地址有独立短期有效期。分享撤销/无效统一返回 SHARE_UNAVAILABLE（404），自然到期可返回 SHARE_EXPIRED（410）。

## 9 使用记录

`GET /records`：type=invoice_batch/tax_calculation/export 可选、date_from/to、cursor/limit，返回 Record 列表。

Record：id（带类型避免冲突）、type、resource_id、title、status、summary、created_at。summary 是对应业务摘要对象，不含完整票据明细。点击后按 type 跳转批次、拆税或导出详情，资源接口为最终状态来源。

## 10 聊天会话和执行

### 10.1 路由

| 方法 | 路径 | 请求/查询 | 返回 |
| --- | --- | --- | --- |
| POST K | /chat/sessions | title 可选 | ChatSession / 201 |
| GET | /chat/sessions | cursor/limit | ChatSession 列表 |
| GET | /chat/sessions/{id} | 无 | ChatSession 快照，含 last_sequence、active_run |
| PATCH | /chat/sessions/{id} | title、expected_version | ChatSession |
| DELETE | /chat/sessions/{id} | 无 | deleted=true；先取消活动 run；不删业务资源 |
| GET | /chat/sessions/{id}/messages | cursor/limit | ChatMessage 列表 |
| POST K | /chat/sessions/{id}/messages | MessageRequest | message_id/run_id/status / 202 |
| GET | /chat/sessions/{id}/events | after_sequence 默认 0、wait_seconds 0..25 默认 20、limit 1..100 默认 100 | EventPage |
| GET | /chat/runs/{id} | 无 | AgentRun |
| POST | /chat/runs/{id}/cancel | cancel_linked_tasks 默认 false | AgentRun，含取消关联资源结果 |
| POST | /chat/actions/{id}/submit | ActionSubmission | AgentRun / 202 |
| GET | /chat/actions/{id}/result | 本人授权、分享仍有效 | ShareCreated |

ChatSession：id/title/version/history_policy/created_at/updated_at/last_sequence/active_run（AgentRun 摘要或 null）/resource_context（只读资源引用）。title 1 至 100 字符。

MessageRequest：client_message_id 唯一字符串、text、file_ids 数组默认空、context 可选；text 和 file_ids 至少一个非空，text 建议最多 10000 字符。附件必须本人 ready；context 仅传当前界面资源引用，不构成执行授权，服务端验证权限。

```json
{
  "client_message_id":"client_msg_001",
  "text":"识别这些 PDF，按销售方整理并导出 Excel",
  "file_ids":["file_001"],
  "context":{"batch_id":null,"selected_invoice_ids":[],"export_id":null}
}
```

响应 data：

```json
{"message_id":"msg_001","run_id":"run_001","status":"queued"}
```

同会话最多一个活动 run，含 waiting 状态。活动期间新普通消息返回 ACTIVE_RUN_EXISTS；对助手问题的回答通过 action submit，不额外创建 run。可先取消原 run 再发新请求。相同 client_message_id 内容不同返回幂等冲突。

ChatMessage：id/session_id/role=user或assistant/client_message_id 可空/text/file_ids/cards/created_at。助手内部工具消息不直接显示给前端。历史按时间降序分页，界面按时间升序排列当前页；事件实时追加不重复插入同 ID。

AgentRun：id/session_id/status/current_step/linked_resources/action 可空/error 可空/created_at/updated_at。linked_resources 为 [{type,id,status}]；current_step 是可展示短描述，不包含模型内部推理。

### 10.2 长轮询事件

返回示例：

```json
{
  "items":[
    {"sequence":1,"run_id":"run_001","type":"run.status","created_at":"2026-10-09T03:00:00Z","payload":{"status":"running","current_step":"创建识别批次"}},
    {"sequence":2,"run_id":"run_001","type":"card.upsert","created_at":"2026-10-09T03:00:01Z","payload":{"card_id":"card_batch_001","type":"invoice_batch","resource_id":"batch_001","data":{"status":"queued"}}}
  ],
  "next_sequence":2,
  "has_more":false
}
```

事件按 sequence 升序，前端处理后保存 next_sequence；按 session+sequence 去重。空结果 next_sequence 保持原游标，继续长轮询。has_more=true 时 wait_seconds=0 立即补拉。超时返回 200 空 items，不表示任务结束。

断线重连使用最后已处理游标；410 EVENT_CURSOR_EXPIRED 时获取会话快照、当前 run、消息和关联资源后，使用快照 last_sequence 继续补拉，快照应在同一一致性边界返回。首期不提供逐 token 事件；正文以 message.created 交付，避免微信端依赖 EventSource。

| type | payload |
| --- | --- |
| run.status | status/current_step |
| message.created | 完整 ChatMessage |
| card.upsert | card_id/type/resource_id 可空/data |
| action.required | 完整 ChatAction |
| action.resolved | action_id/status/decision |
| run.completed | status/result_resource_ids |
| run.failed | error |

### 10.3 卡片

卡片公共字段 card_id/type/resource_id/data，同 card_id 更新而非追加。

| type | data 与前端操作 |
| --- | --- |
| invoice_batch | Batch 或摘要；查看详情、重试、取消 |
| invoice | InvoiceSummary；查看原文件、核对 |
| tax_calculation | TaxCalculation；查看记录 |
| export | Export；生成中展示进度，available 才下载 |
| share | Share 首次创建结果；调用系统分享、撤销 |
| file_picker | action_id/purpose/accepted_formats；前端选文件并上传后向该 action 提交 file_ids |
| confirmation | ChatAction；展示范围、变更和有效期，再提交选择 |
| choice | ChatAction；从明确候选中选择，不自造资源 ID |
| ui_action | name=contact_customer_service/open_download/open_share/logout/wechat_authorization；由前端执行授权过的 UI 动作 |

ui_action 中下载/分享引用资源 ID，不直接信任任意模型 URL。contact 调用微信客服；logout 成功后清除客户端 token。下载卡片点击时请求最新下载 URL，不长期使用事件中的签名地址。

### 10.4 澄清和确认

ChatAction：id/run_id/type=input或confirmation/prompt/summary/input_schema/options/expires_at/status。schema 为服务端受限 JSON Schema，只用于表单字段描述，不执行脚本；type input 允许税率、日期、候选资源和已上传 file_ids 等明确参数。

示例：

```json
{
  "id":"action_001",
  "run_id":"run_001",
  "type":"confirmation",
  "prompt":"创建可凭链接访问的分享？",
  "summary":{"operation":"create_share","export_id":"exp_001","filename":"发票汇总.xlsx","expires_in_days":7},
  "input_schema":null,
  "options":[{"value":"confirm","label":"创建分享"},{"value":"cancel","label":"取消"}],
  "expires_at":"2026-10-09T03:10:00Z",
  "status":"pending"
}
```

confirmation 提交：

```json
{"decision":"confirm"}
```

input 提交：

```json
{"decision":"submit","values":{"tax_rate":"0.13"}}
```

confirmation 仅允许 confirm/cancel，input 允许 submit/cancel；values 必须符合 action 当前 schema。拒绝未知字段和其他资源，权限再次检查。action 过期 410，资源版本变化 409；服务端将旧 action 失效并创建替代 action，通过 action.required 事件返回。前端刷新 run 后展示新 action，不偷偷执行变更后的范围。

取消分享确认表示不执行分享，可继续其他步骤；取消缺少关键参数的 input 默认取消该 run。返回状态及 message 明确说明。确认提交 202 仅代表受理，最终结果由事件或 run 查询获得。

### 10.5 全功能与前端配合

| 用户目标 | 后端工具与 UI |
| --- | --- |
| 拍照/选择文件识别 | file_picker → 上传 → create_invoice_batch |
| 识别并导出 | 批次 → waiting_task → 查询 → 快照导出 → 文件卡片 |
| 拆税 | 补齐金额税率 → calculate_tax → 结果卡片 |
| 修正发票 | 明确字段+版本 → 更新；推测/批量修改先确认 |
| 查历史/工具/帮助 | 查询工具，分页返回摘要 |
| 分享 | 展示范围及期限 → 授权 → create_share → 微信分享 UI |
| 改设置/提交反馈 | 使用同一业务服务，不只口头回复 |
| 安全/注销/客服 | 安全查询；注销确认且能力启用；微信客服 UI |

聊天关闭不取消业务任务。助手失败可以保留已完成业务结果，linked_resources 告诉用户哪些完成、哪些未完成；模型服务不可用时功能页面仍可操作。

## 11 页面与接口映射

| 页面 | 主要接口 |
| --- | --- |
| /pages/home/index | GET tools |
| /pages/tools/index | GET tools |
| /pages/invoice/index | 上传、POST/GET invoice-batches、GET content/tips/invoice |
| /pages/invoice/detail | GET batch/items/invoices、修正/确认/重试/取消 |
| /pages/tax-split/index | GET config、POST/GET tax-calculations |
| /pages/tax-split/detail | GET tax-calculations/{id} |
| /pages/export/index | GET/POST exports、详情/下载/share、help article |
| /pages/profile/index | GET users/me、users/me/tools、records、exports |
| /pages/profile/feedback | help/categories、feedback |
| /pages/profile/security | security、logout、deletion-requests |
| /pages/profile/notification | notification-settings |
| /pages/profile/privacy | privacy-settings |
| /pages/profile/about | app/about、微信客服 |
| /pages/records/index（新增） | GET records |
| /pages/chat/index（新增） | chat session/message/events/run/action、附件上传 |
| /pages/share/index（新增） | 公开 shares/{token} 和下载 |

原文档未提供 records/chat/share 页面路由，以上为新增建议，前端实施时统一确认。

## 12 联调顺序和验收

1. 对齐环境地址、微信 AppID、域名、测试账号；后端发布实际 OpenAPI 和测试数据。
2. 登录/刷新/登出与跨用户权限验证。
3. 真机 PDF、图片分片上传、暂停续传、验证失败及 ready 状态。
4. 多页、一页多票、混合类型、待核对、部分失败和重试。
5. 拆税配置、113 元/13%、舍入边界、重复提交和配置变更。
6. 快照导出、中文 Excel/PDF、待核对标注、过期下载、分享撤销。
7. 聊天连续流程、澄清、确认、事件去重、断线、取消及任务恢复。
8. 内容、用户设置、反馈和所有已启用功能的助手映射。

| 验收场景 | 预期 |
| --- | --- |
| 同键重复创建批次/导出/消息 | 一个资源，无重复副作用 |
| 其他用户访问 ID | 404，无内容泄露 |
| 大 PDF | 不因产品总页数限制拒绝，分段显示进度 |
| 单页失败 | 其他结果可查看，重试只处理失败单元 |
| 修正时版本落后 | 409，不覆盖新值 |
| 导出后修改票据 | 已有导出不变 |
| 签名地址过期 | 重新授权，资源 ID 不变 |
| 分享撤销 | 不再签发新地址，已发短期地址边界有说明 |
| 聊天关页/断线 | 后台继续，游标恢复，无重复卡片 |
| 附件要求忽略权限 | 不影响系统工具和权限 |
| 助手声称导出成功 | 必须有 succeeded 且文件 available 的真实 Export |
| 模型故障 | 页面业务仍可用，已创建资源可查询 |

实施时每个路由绑定 Pydantic 请求/响应模型，形成实际 OpenAPI。未通过验收的接口在能力配置中关闭，避免前端使用尚未实现的占位能力。


## 13 当前实现的补充约定

- 新增 `/auth/dev-login`，仅显式启用开发登录时可用，请求为 account 字符串；不会在生产开放。
- 本地开发签名上传 PUT `/storage/parts?token=...` 返回原样 ETag；完成上传提交相同值，最大长度 128。
- 本地开发下载 GET `/storage/download?token=...` 返回实际文件，内部传输接口不列入公开业务 OpenAPI。
- 新错误 PART_SIZE_MISMATCH（422）表示单个分片长度不符合会话计划，不表示产品文件总大小受限。
- 本轮幂等记录保留 24 小时，暂不返回可选 Idempotency-Expires-At 响应头；超过期限按新操作处理。
- SQLite 并发竞争可能返回 STATE_CONFLICT（409）或 SERVICE_UNAVAILABLE（503），客户端复用原幂等键重试；成功副作用不会重复。
- 金额计算与确认/分享规则仍区分建议默认值和已确认需求，生产政策在上线前确定。


### 13.1 第二阶段已实现（0.2.0）

- 第 6 节的全部批次与票据接口已实现，真实供应商需配置后开启；具体流程见 [发票对接指南](invoice-integration.md)。
- /capabilities 的 invoice_types 返回 code、label、recognition_status；当前均为 adapter_only，不表示真实样本已验证。invoice_recognition_enabled 根据 OCR 配置返回。
- uploads.storage_backend 为 local/cos。COS technical_limits 公开分片数量和按单片容量计算的技术上限；product_max_size_bytes/product_max_pdf_pages 仍为 null。
- COS 分片地址带返回的 Content-Type；提交原始字节并保留原样 ETag，不使用表单包装。后台核验云端实际分片而非仅相信客户端清单。
- 新错误 INFRASTRUCTURE_FILE_LIMIT（413）、NO_INVOICE_FOUND（单元失败）、PARTIAL_OCR_FAILURE（单元部分成功）、OCR_PROVIDER_ERROR、STORAGE_BACKEND_MISMATCH 等在资源 error 或统一错误响应中给出。
- 列表 keyword 当前匹配票据号码或销售方名称，不包含扩展字段全文检索。invoice_date_from/to 为票面日期，与创建日期筛选独立。
- PATCH 对提交成功的修正增加 version 并重新核对；同值修正也保留审计。buyer/seller/lines 不接受整体 null，具体金额字段可 null。
- /records 已聚合 tax_calculation 和 invoice_batch；export 筛选当前为空，导出业务未实现。
- 拆税 source_invoice_id 已启用，必须归属本人；它只是来源关联，不自动代替用户选择金额和税率。
- 生产数据库并发、真实云凭证、真实样本和微信真机尚未验证；聊天接口与导出分享仍属于目标契约。


### 13.2 第三阶段已实现（0.3.0）

- 第 8 节全部导出和分享接口已实现，export_enabled=true；第 10 节聊天接口仍待开发。
- 快照在导出创建事务内完成，202 已返回确定的 record_count/pending_review_count/snapshot_at；后台只读取该快照生成文件。没有结果返回 EMPTY_EXPORT，不建立空任务。
- 文件访问期限由 FT_EXPORT_RETENTION_DAYS 控制，默认 7 天，从成功生成时计；Share.expires_at 为请求期限与文件期限的较早者。physical 文件/快照清理不是访问过期接口的行为。
- 分享表只保存 token 哈希；24 小时幂等响应保存认证加密的凭证，原键可恢复同一创建响应，普通分享查询不能恢复 token。密钥轮换可能返回 SHARE_REPLAY_UNAVAILABLE。
- 分享状态查询为当前状态依据；原创建请求重放返回原始响应，即使分享随后被撤销，公开访问仍会重新校验并拒绝。
- download-url 有独立短期期限，并以剩余文件/分享期限缩短。本地下载重检分享状态；COS 已签地址在自身期限内可能仍有效，撤销阻止新的签发。
- Excel 超过 15 位有效数字的数据保留精确文本，超过单表行数另开续表；未知值为空、零值保留，UNKNOWN 币种不做金额合计。
- group_by=month：票据按票面日期，拆税按北京时间创建月份；来源字段不明时单列待核对。
- /records 已聚合 export；普通 /files 仍用于上传原件，导出文件通过 /exports/{id}/download-url 获取，避免将导出当成上传文件。
- 当前没有导出 retry 接口。失败后可用新键新建任务；临时存储错误会由 Worker 自动退避重试。取消后已有来源和快照保留。
- 实际联调步骤与文件内容见 [导出与分享指南](export-integration.md)。真实云资源、原生 Excel 和微信真机尚未验证。


### 13.3 第四阶段已实现（0.4.0）

聊天 12 个路由已实现，新增类型化 SessionOut/RunOut/ActionOut/MessageOut/EventPage，完整 OpenAPI 共 67 个操作。LangGraph 编排、40 个工具、长轮询、澄清/确认、任务等待和跨进程恢复已实现；真实模型默认为 disabled，配置后开启。

第 10 节是目标契约；本版本准确对接以 [聊天对接指南](chat-integration.md) 和 OpenAPI 为准，主要差异如下：

- session 增加 expires_at；关闭历史时 history_policy=ephemeral，24 小时后不可读，由 Worker 删除内容。
- run 增加 usage；取消直接到 cancelled，无可观察的 cancelling 阶段；返回 cancelled_linked_resources。
- 当前普通消息不自动转换自然语言批准或澄清，回答通过 action submit；同会话一个活动 run。
- 所有票据/资料/设置修改均确认；input.kind 支持 text/tax_rate/file_ids/choice。客户端不提供任意 JSON Schema。
- Action 增加 result_available；分享卡片不含 token，点击时从 /chat/actions/{id}/result 取已加密保存的结果。
- ACTION_STALE 返回 409，data 为带替代 action 的 run；行动过期返回 410。确认后执行前也会复核范围。
- 会话事件尚未单独裁剪，不返回 EVENT_CURSOR_EXPIRED；删除或会话到期为 404。越界 sequence 为 400 INVALID_CURSOR。
- privacy retention_policy.chat_days 在关闭历史时为 1，retained 历史期限仍为 null，需制定生产政策。

真实模型、云服务、PostgreSQL 并发、微信真机及上线限流/保留政策尚未验证；不能仅根据这些接口和本机测试宣布上线。
