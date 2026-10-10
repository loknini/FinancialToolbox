TOOLS = [
    {
        "id": "invoice",
        "code": "invoice",
        "name": "发票识别",
        "description": "多类型票据识别",
        "route": "/pages/invoice/index",
        "enabled": False,
        "sort_order": 1,
        "icon_url": None,
    },
    {
        "id": "tax",
        "code": "tax",
        "name": "拆税计算",
        "description": "从含税金额计算不含税金额及税额",
        "route": "/pages/tax-split/index",
        "enabled": True,
        "sort_order": 2,
        "icon_url": None,
    },
    {
        "id": "export",
        "code": "export",
        "name": "导出中心",
        "description": "生成 Excel 或 PDF 报告",
        "route": "/pages/export/index",
        "enabled": True,
        "sort_order": 3,
        "icon_url": None,
    },
]
CATEGORIES = [
    {"code": code, "name": name}
    for code, name in [
        ("invoice", "票据识别"),
        ("tax", "拆税计算"),
        ("export", "导出文件"),
        ("account", "用户账号"),
        ("other", "其他问题"),
    ]
]
ARTICLES = [
    {
        "slug": "export-formats",
        "category": "export",
        "title": "导出格式说明",
        "body": "Excel 用于整理汇总和明细；PDF 为汇总报告，与原始发票文件不同。导出采用创建时的数据快照，待核对数据会标注，分享需要主动创建。",
        "body_format": "text",
        "version": 1,
        "updated_at": "2026-10-09T00:00:00Z",
    },
    {
        "slug": "tax-rounding",
        "category": "tax",
        "title": "拆税计算规则",
        "body": "不含税金额按含税金额除以一加税率计算，四舍五入到两位；税额为含税金额与不含税金额之差。",
        "body_format": "text",
        "version": 1,
        "updated_at": "2026-10-09T00:00:00Z",
    },
]
