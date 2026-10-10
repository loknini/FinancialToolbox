"""Generate synthetic QA exports and PDF page images; no user data."""

import json
from pathlib import Path
from uuid import uuid4

import pymupdf
from PIL import Image, ImageDraw
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Base, Export, ExportSnapshot, User
from app.services import export_render


def main():
    root = Path(__file__).resolve().parents[1]
    directory = root / "var" / "export-layout-qa"
    directory.mkdir(parents=True, exist_ok=True)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = Settings()
    with Session(engine) as db:
        user = User(appid="qa", openid=str(uuid4()))
        db.add(user)
        db.flush()
        export = Export(
            user_id=user.id,
            format="pdf",
            source_type="invoices",
            request_data={"group_by": "seller", "review_scope": "all"},
            record_count=38,
            pending_review_count=19,
        )
        db.add(export)
        db.flush()
        for index in range(38):
            seller = "示例供应商甲" if index % 2 == 0 else "示例供应商乙"
            if index == 37:
                seller = "长文本布局验证：" + ("票据原件需要核对，保留全部信息。" * 40)
            data = {
                "id": str(uuid4()),
                "type_label": "数电发票（示例）",
                "invoice_number": f"EXAMPLE{index:04}",
                "invoice_code": "DEMO",
                "invoice_date": "2026-10-09",
                "seller": {"name": seller, "tax_id": "DEMO-TAX"},
                "buyer": {"name": "示例购买方", "tax_id": "DEMO-BUYER"},
                "gross_amount": "9999999999999999999999.99" if index == 0 else "113.00",
                "net_amount": None if index == 0 else "100.00",
                "tax_amount": "0.00" if index == 0 else "13.00",
                "currency": "UNKNOWN" if index == 0 else "USD" if index == 1 else "CNY",
                "review_status": "pending_review" if index % 2 == 0 else "confirmed",
                "issues": [{"message": "号码或币种需要核对"}] if index % 2 == 0 else [],
                "source": {"file_id": "SYNTHETIC-QA", "page_from": index + 1},
                "lines": [
                    {
                        "line_number": 1,
                        "name": "示例服务",
                        "quantity": "1",
                        "unit_price": "100.00",
                        "net_amount": "100.00",
                        "tax_rate": "0.13",
                        "tax_treatment": "taxable",
                        "tax_amount": "13.00",
                    }
                ],
            }
            db.add(ExportSnapshot(export_id=export.id, position=index, data=data))
        db.flush()
        pdf = directory / "invoice-qa.pdf"
        xlsx = directory / "invoice-qa.xlsx"
        export_render.pdf(db, export, pdf, settings)
        export_render.excel(db, export, xlsx)
    pages = []
    with pymupdf.open(pdf) as document:
        for index, page in enumerate(document):
            path = directory / f"page-{index + 1}.png"
            page.get_pixmap(matrix=pymupdf.Matrix(1.3, 1.3)).save(path)
            pages.append(path)
    contact = Image.new("RGB", (700, 300 * ((len(pages) + 1) // 2)), "#dddddd")
    draw = ImageDraw.Draw(contact)
    for index, path in enumerate(pages):
        with Image.open(path) as original:
            original.thumbnail((344, 267))
            x = (index % 2) * 350
            y = (index // 2) * 300
            contact.paste(original, (x, y + 20))
            draw.text((x + 5, y + 3), f"Page {index + 1}", fill="black")
    contact.save(directory / "contact.png")
    print(
        json.dumps(
            {"pages": len(pages), "qa_directory": str(directory), "synthetic_data_only": True}
        )
    )
    engine.dispose()


if __name__ == "__main__":
    main()
