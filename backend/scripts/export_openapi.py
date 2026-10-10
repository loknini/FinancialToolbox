import json
from pathlib import Path

from app.main import create_app


def main():
    app = create_app()
    target = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"
    target.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    app.state.engine.dispose()
    print(f"OpenAPI exported: {target}")


if __name__ == "__main__":
    main()
