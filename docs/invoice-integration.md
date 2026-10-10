# 发票与云服务对接指南（0.2.0）

本文描述当前可调用代码。完整接口见 [接口契约](api-contract.md)，实际请求字段与响应模型见 [OpenAPI](openapi.json)。SDK 适配已完成，未使用真实凭证验证云端调用。

## 一、服务端配置

API 与 Worker 使用同一份 .env、数据库、存储模式和 OCR 配置。设置变化后重启两者，不在未迁移原文件时直接切换存储后端。密钥只放服务端；前端只接收短期授权 URL。

本地 .env 可继续使用 FT_STORAGE_BACKEND=local；它适合一个 API 进程进行本地联调。开启云直传前，在腾讯云准备私有 COS 存储桶，设置：

```dotenv
FT_STORAGE_BACKEND=cos
FT_COS_BUCKET=你的存储桶名称-APPID
FT_COS_REGION=ap-guangzhou
FT_COS_SECRET_ID=服务端密钥ID
FT_COS_SECRET_KEY=服务端密钥
FT_COS_PREFIX=financial-toolbox/
```

在腾讯云开通票据识别高级版接口并赋予对应 API 权限后设置：

```dotenv
FT_OCR_PROVIDER=tencent
FT_OCR_SECRET_ID=服务端密钥ID
FT_OCR_SECRET_KEY=服务端密钥
FT_OCR_REGION=ap-guangzhou
FT_OCR_TIMEOUT_SECONDS=60
```

两个服务可独立启用：本地存储也可以接真实 OCR，COS 上传也可以在 OCR 关闭时验证。未配置的模式保留 FT_OCR_PROVIDER=disabled，不产生假识别数据。

COS 权限至少覆盖当前前缀的初始化分片、上传分片签名、查询分片、合并/终止分片、查询对象和下载。保持桶私有，设置清理遗留分片的生命周期；生命周期按上传初始化时间计，应用续期不改变云端生命周期。若使用 H5 联调，配置受限域名 CORS 并暴露 ETag；微信真机需要在小程序后台配置 HTTPS request/uploadFile/downloadFile 合法域名。

不要把密钥、微信 code、Bearer token 或完整签名 URL 放进日志、工单和样本清单。使用 README 的无访问日志启动方式。

## 二、前端流程

1. GET /api/v1/capabilities，读取上传格式与 invoice_recognition_enabled；能力关闭时展示未配置状态。
2. POST /upload-sessions：filename、content_type、字符串 size_bytes、purpose=invoice。提供 Idempotency-Key，保存会话 ID。
3. 读取服务端 part_size_bytes，按该大小切片；POST /upload-sessions/{id}/part-urls 获取每片短期地址。按返回 method 和 headers 发送原始分片字节，不能再包装 multipart/form-data。
4. 记录每片的原样 ETag。暂停恢复时 GET 上传会话；COS 模式由后端查询真实分片，客户端提交的进度不是完成依据。
5. POST /upload-sessions/{id}/complete 提交清单与幂等键。202 只表示后台校验已排队；查询 /files/{id} 直到 ready 或 rejected。
6. ready 后 POST /invoice-batches，提交非空 file_ids、source=page/chat 和幂等键，保存批次 ID。
7. 查询批次和 items；页码从 1 开始。发现尚未完成时 total_units/percent 为 null，展示分析中；100% 包括成功、失败或取消，必须继续读取 status。
8. GET /invoices?batch_id=... 查询摘要；按需请求详情和来源文件 download-url，展示待核对问题及原件。
9. PATCH 票据携带 expected_version，只传修改字段。buyer/seller 局部合并，lines 整体替换。修改后回到 pending_review；再 POST /confirm 携带返回的新版本。
10. 错误级别的金额/字段问题未解决时确认返回 REVIEW_REQUIRED。批次 failed/partial_failed 可 POST /retry {"scope":"failed"}，使用新操作的幂等键；同一次请求重试复用该键。

用户离开页面不取消任务；只有明确 POST /cancel 才取消批次。正在调用供应商的任务可能稍后结束，取消后的响应不再入库为新票据。原件和已完成票据不会因为取消而删除。

所有路径带 /api/v1 前缀；除能力目录等公开接口外，均带 Authorization: Bearer。与原文档一样，非本人资源返回 404。批次列表和票据列表按创建时间倒序；单元列表按创建批次时的文件顺序、页码升序。游标绑定当前用户、筛选和资源，修改筛选后重新分页。

## 三、文件和供应商范围

- 上传 PDF、JPEG、PNG；不设置产品总大小或 PDF 页数上限。OFD、XML、ZIP、Office 文件尚未接入，不能仅凭扩展名认作支持。
- COS 最多 10000 个分片、单片最多 5 GiB；大文件会提高分片大小，超过当前云端技术容量返回 INFRASTRUCTURE_FILE_LIMIT。能力接口公开技术边界，客户端仍受微信/操作系统/网络条件约束。
- 加密 PDF、无页 PDF、实际格式与声明不一致或损坏文件不能识别。图片仍受图像解码器的安全读取能力限制。
- 使用 RecognizeGeneralInvoice，每次提交一个 PDF 页；单页输入过大时转为 JPEG，必要时压缩，保留原始文件。转换可能影响小字质量，需要原件人工核对。
- 供应商区域坐标来自单页识别输入；发生转图时不承诺与原 PDF 坐标相同。原始响应保留转换标记与来源页码。
- 当前会将原件读取到 Worker 临时目录，COS 文件按单元下载并在处理后清理。大文件多页的下载、哈希和解析性能还需云端压测，再决定缓存及扩容；没有宣称无限容量或完成性能验证。

## 四、标准化和核对

常规结构化票据映射号码、开票日期、购买/销售方、票面总额及供应商明确给出的不含税额/税额。增值税和数电票据提取明细；机打/卷票等明细金额未明确不含税时不填为 net_amount，其余票种尽可能取公共字段，完整原始字段保存在 extended_fields，无法安全对应的字段不推断。

金额使用十进制字符串，未知值为 null，票面 0 保留 "0.00"，红字负金额可保存。机票中的机场税/费不会直接当作增值税税额；没有明确 TaxAmount 时税额留空。火车出发日期不直接当作开票日期。免税、不征税和零税率分别标识。

currency 只读，取供应商明确字段；国内常规票据默认 CNY，海外票据币种不明时为 UNKNOWN。当前没有汇率换算，后续导出必须区分币种，不能直接合计不同币种。

gross_amount 缺失或 net_amount + tax_amount 与 gross_amount 不一致产生 error；未知票种、未知币种、缺号码或日期产生 warning。不是税务真伪查验，亦不自动依据拆税公式改写票面金额。修正使用乐观锁并保留前后值、原因、操作者，首次 OCR 数据及原始响应保留。

重试去重依据为单元、标准化类型、原始号码/代码；无号码时使用结果位置。已存在结果保持原样，后续新增成功项目追加。识别结果号码或顺序改变仍可能形成不同结果，真实样本去重与提示需要进一步验证。供应商请求中断后重新调用可能再次计费，不能把业务幂等误认为云端不重复计费。

## 五、票种能力清单

adapter_only 表示映射已接入，但没有真实脱敏样本证明字段准确率。当前没有任何票种标记为 sample_verified。医疗、海外、小票、银行回单等扩展类型的结构化字段可能不全；出现缺失时需人工核对，扩展字段仍保存原始供应商内容。

| code | 类型 | 当前验证状态 |
| --- | --- | --- |
| taxi_ticket | 出租车票 | adapter_only，未验证真实样本 |
| quota_invoice | 定额发票 | adapter_only，未验证真实样本 |
| train_ticket | 火车票 | adapter_only，未验证真实样本 |
| vat_invoice | 增值税发票 | adapter_only，未验证真实样本 |
| air_transport | 机票行程单 | adapter_only，未验证真实样本 |
| machine_printed | 机打发票 | adapter_only，未验证真实样本 |
| bus_ticket | 汽车票 | adapter_only，未验证真实样本 |
| shipping_ticket | 轮船票 | adapter_only，未验证真实样本 |
| vat_roll | 增值税卷票 | adapter_only，未验证真实样本 |
| vehicle_invoice | 机动车票据 | adapter_only，未验证真实样本 |
| toll_invoice | 通行费票据 | adapter_only，未验证真实样本 |
| non_tax_bill | 非税票据 | adapter_only，未验证真实样本 |
| digital_invoice | 数电发票 | adapter_only，未验证真实样本 |
| medical_bill | 医疗票据 | adapter_only，未验证真实样本 |
| tax_payment | 完税凭证 | adapter_only，未验证真实样本 |
| customs_payment | 海关缴款 | adapter_only，未验证真实样本 |
| bank_slip | 银行回单 | adapter_only，未验证真实样本 |
| taxi_itinerary | 网约车行程 | adapter_only，未验证真实样本 |
| customs_declaration | 报关单 | adapter_only，未验证真实样本 |
| overseas_invoice | 海外票据 | adapter_only，未验证真实样本 |
| shopping_receipt | 购物小票 | adapter_only，未验证真实样本 |
| sales_inventory | 销货清单 | adapter_only，未验证真实样本 |
| toll_summary | 通行费汇总 | adapter_only，未验证真实样本 |
| unknown | 其他票据 | adapter_only，未验证真实样本 |

上线前按每类收集脱敏的清晰 PDF、扫描 PDF、照片、多票混合、红字和缺字段样本；记录页数、实际票数、应识别字段、供应商请求 ID、识别结果、差异和核对结论。不能把模拟测试通过当成 OCR 准确率。

## 六、验证与参考

自动化测试使用官方 SDK 的模型转换和注入客户端，覆盖请求分片、逐页输入、SDK 错误、原始字段保留、金额、部分失败及恢复，不访问真实云资源。

官方参考：[票据识别高级版](https://cloud.tencent.com/document/product/866/90802)、[COS Python 分片上传](https://cloud.tencent.com/document/product/436/65820)。实际依赖版本由 backend/uv.lock 固定。
