# 聊天助手对接指南（0.4.0）

本阶段已实现 12 个聊天 HTTP 操作、40 个受控工具和 LangGraph 状态编排。准确请求/响应类型见 [OpenAPI](openapi.json)，业务能力见 [开发进度](development-status.md)。所有聊天接口要求 Bearer 登录；会话创建和发消息还要求 Idempotency-Key。

## 1 配置和启动

先执行数据库迁移，再同时运行 API 与 Worker。迁移 head 为 fc350caf6e68。API 与 Worker 必须使用同一数据库、签名密钥和模型配置；API 不在请求内执行模型或 OCR。

默认 FT_MODEL_PROVIDER=disabled，/capabilities.chat.enabled=false；可管理会话，发消息返回 403 CAPABILITY_DISABLED，不返回虚假的助手成功响应。启用示例：

```dotenv
FT_MODEL_PROVIDER=chat_completions
FT_MODEL_BASE_URL=https://供应商基础地址/v1
FT_MODEL_NAME=供应商提供的支持工具调用的模型名称
FT_MODEL_API_KEY=服务端密钥
FT_MODEL_TIMEOUT_SECONDS=60
FT_MODEL_MAX_OUTPUT_TOKENS=2048
FT_AGENT_MAX_MODEL_CALLS=12
FT_AGENT_MAX_TOOL_CALLS=30
FT_AGENT_MAX_TOTAL_TOKENS=100000
FT_AGENT_MAX_CONTEXT_CHARS=100000
FT_AGENT_RUN_SECONDS=86400
FT_CHAT_ACTION_SECONDS=600
```

基础地址按供应商文档配置，后端追加 /chat/completions；不要重复填此路径。兼容工具调用的协议不意味着所有供应商都已实测。生产只接受 HTTPS；凭证只放服务端，不发给小程序。首期使用标准非思考模式的工具调用；供应商特有参数或思考消息协议需要扩展适配器后验证。非流式、不传图片原件，PDF/图片先走已有上传与 OCR 流程。

模型格式依据 [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) 的 messages/tools/tool_calls/usage 协议适配，供应商和具体模型尚未确定。真实模型凭证、中文意图准确率、价格及供应商数据处理政策仍待评估。本机测试使用 HTTP 模拟模型。

## 2 前端基本流程

1. POST /chat/sessions，body 为 {"title":"发票整理"}，得到 session.id。
2. POST /chat/sessions/{id}/messages，立即返回 202，保存 message_id、run_id。
3. GET /chat/sessions/{id}/events，以 after_sequence 长轮询，显示状态、消息、卡片和操作表单。
4. 遇到 action.required，展示服务端 summary 与 input_schema，再提交到 action submit。
5. run.completed/run.failed 后解除输入锁；GET run 可随时校准状态。

发送消息示例：

```json
{
  "client_message_id":"客户端稳定的消息编号",
  "text":"识别这些文件，整理后导出 Excel",
  "file_ids":["本人已验证的file_id"],
  "context":{"batch_id":null,"selected_invoice_ids":[],"export_id":null,"tax_calculation_id":null}
}
```

text 最多 10000 字符；每条消息最多 100 个附件、100 个选中票据引用，引用必须本人可访问。附件必须 ready，purpose 为 invoice/chat_attachment，未到访问期限。这里的消息容量限制不改变上传文件大小/PDF 页数的产品规则。

context 为空时沿用会话已验证引用；传空对象可清空。context 只提供界面引用，不授予修改/分享权限。模型在资源不明确时应询问用户，不能自动选最新一条代替用户选择。

同会话只能有一个活动 run，包含 waiting 状态。新普通消息返回 409 ACTIVE_RUN_EXISTS。当前实现通过 action submit 回答澄清或确认，不自动将自由文本“确认”当成批准。前端可以将用户在聊天输入框中对当前问题的回答映射到 action 表单，但必须只提交当前 action 允许的字段。

相同 Idempotency-Key/参数在 24 小时内重放初次响应；相同 client_message_id/内容在会话存续期间仍不会创建第二个 run，换 key 也一样。同编号不同内容为 IDEMPOTENCY_CONFLICT。重放时状态可能是初次状态，随后查询 run 获取最新状态。

## 3 run 与任务恢复

实际状态为 queued/running/waiting_input/waiting_confirmation/waiting_task/succeeded/failed/cancelled。取消在服务端锁住会话后直接变为 cancelled，首期不对外停留在 cancelling。

GET /chat/runs/{id} 返回 current_step、linked_resources、action、error、usage 和时间。current_step 是可展示的步骤描述，不是模型内部推理。usage 是已接受模型响应的 tokens/调用次数，不等于供应商最终账单；取消或丢弃的外部请求仍可能产生供应商费用。

识别/导出工具创建后台任务后 run 进入 waiting_task，Worker 约每 5 秒检查并继续编排；后台任务 partial_failed/failed/cancelled 也作为真实结果交给模型处理。无需一直打开页面或保持连接。

Worker 使用租约、心跳、tick 标识与写回检查防止过期执行者写回；业务操作、执行步骤、图检查点和事件在同一事务提交。等待任务无变化时复用原轮询任务，不反复生成检查点或累计重试次数。模型超时/限流按最多 3 次任务尝试退避重试；配置错误、格式错误不重试。不能保证外部模型调用只发生一次，成功业务副作用通过事务状态和稳定操作键防重。

POST /chat/runs/{id}/cancel：

```json
{"cancel_linked_tasks":false}
```

默认只停止助手，已创建的识别/导出继续。true 同时请求取消 linked_resources 中的识别批次/导出，响应含 cancelled_linked_resources；已结束任务仍返回其当前状态，不承诺删除文件。

## 4 澄清与确认

GET run.action 或 action.required 事件返回：id/run_id/type/status/prompt/summary/input_schema/options/expires_at/result_available。

input 支持四种服务端定义的输入：

| summary.kind | values |
| --- | --- |
| text | {"text":"明确补充内容"} |
| tax_rate | {"tax_rate":"0.13"}，仅接受配置枚举中的小数字符串 |
| file_ids | {"file_ids":["本人ready文件"]}，先通过现有上传接口上传 |
| choice | {"resource_id":"候选枚举中的ID"}，服务端再次检查归属 |

```json
{"decision":"submit","values":{"tax_rate":"0.13"}}
```

confirmation 只接受 confirm/cancel 且 values 必须为空：

```json
{"decision":"confirm"}
```

当前版本对票据修改/核对、个人资料/设置修改、分享创建/撤销和退出 UI 动作使用确认。拆税、识别、导出、明确反馈可直接调用。确认摘要由服务端从实际资源、版本和工具参数构建，客户端不能替换隐藏参数。

重复相同提交返回当前 run，不重复执行；改成其他选择返回 ACTION_ALREADY_CONSUMED。取消 input 结束 run；取消 confirmation 跳过该操作，助手可继续剩余步骤。

版本或分享范围变化时返回 409 ACTION_STALE，data 为当前 run（含替代 action），同时产生 action.resolved/action.required 事件。重新展示新的实际范围。执行前再次核验版本；即使已点击确认，期间资源变化仍会生成新的 action。

action 默认 10 分钟到期。提交过期 action 为 410 ACTION_EXPIRED，过期状态和事件保留；无人提交时 Worker 到期结束等待。整体 run 默认 24 小时期限，超过期限会失败；不会自动取消其他业务任务。

## 5 卡片和分享凭证

card.upsert 使用 card_id 替换已有卡片；消息中的 cards 是此 run 的最终卡片快照。业务卡片基于真实服务结果，包括 tax_calculation/invoice_batch/invoice/export/share，另有 confirmation/choice/file_picker/ui_action。卡片金额均为字符串，前端不要重新用浮点数计算。

- file_picker：data 中含 action 对象；选文件并上传后向该 action 提交 file_ids。
- confirmation/choice：展示 action 的实际摘要和候选；已解决卡片由 action.resolved 或查询 run 更新。
- export：仅 file.status=available 才显示下载；点击时调用既有 /exports/{id}/download-url。
- share：data 含 action_id/result_available，不在事件里长期保存 token。
- ui_action：name 为 contact_customer_service/open_download/open_share/logout/wechat_authorization，由小程序执行；后台不会绕过微信授权、代替用户选择本地文件或自动退出登录。

成功分享后，本人请求：

GET /chat/actions/{action_id}/result

返回 ShareCreated（token/share_path 等）。该结果加密保存，不能被其他用户读取；读取时仍检查分享是否到期/撤销。点击系统分享前获取此结果，不把访问凭证写入埋点、控制台或普通事件缓存。退出卡片由前端调用既有退出接口，再清除本地登录态。

公开分享只允许已生成文件，摘要明确文件、记录数、待核对数、期限及“持链接即可下载”的范围。执行后的绝对到期时间不会晚于用户确认的时间。密钥轮换会使旧加密结果不可恢复，需要配套迁移或重新分享。

## 6 事件与历史

GET /chat/sessions/{id}/events 参数：after_sequence>=0、wait_seconds=0..25（默认20）、limit=1..100（默认100）。按 sequence 升序；以 session+sequence 去重，保存 next_sequence。has_more=true 时立即 wait_seconds=0 补拉；空页保持游标。越界游标为 400 INVALID_CURSOR。

事件类型：run.status、message.created、card.upsert、action.required、action.resolved、run.completed、run.failed。首期没有 token 流、SSE 或 WebSocket。事件读取会重新验证登录态，不在等待期间长期占数据库事务。

GET /sessions/{id} 返回 last_sequence/active_run/resource_context；GET /messages 为倒序分页，UI 自行按时间升序排列。断线后先获取快照与消息，再从已处理游标补拉，消息按 id 去重。当前保留会话内全部事件，不实现独立事件裁剪；删除/会话到期返回 404，而不是独立 EVENT_CURSOR_EXPIRED。

创建会话时读取 retain_chat_history：开启为 retained；关闭为 ephemeral，24 小时后不可访问，Worker 分批物理删除会话内容。关闭开关只作用于未来会话，既有会话不变。Worker 停止时物理清理会延后，但过期会话立即不可读。

DELETE /sessions/{id} 删除消息、run、步骤、actions、检查点和事件，清空会话标题/引用及聊天幂等缓存，仅保留空墓碑；执行中的旧 Worker 不能复活会话。已有票据、拆税、导出和分享继续存在。数据库备份和底层空间回收另行管理。retained 历史的统一生产保留期限仍待确定。

## 7 当前验证边界

已验证受控模型协议、工具/归属/参数校验、澄清与确认、资源版本变化、步骤和检查点回滚、旧租约写回阻止、取消、事件分页、PDF 多页识别链路（OCR 模拟）、导出与分享。独立本机 API 重启、新 Worker 进程逐步执行的 HTTP 联调通过。

真实模型语义评测、真实云 OCR/COS、真实 PostgreSQL 并发与恢复、微信真机、用户级限流及上线保留策略仍待完成。聊天生成成功或测试通过不代表这些环境已验证。
