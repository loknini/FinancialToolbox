import secrets
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    target = root / ".env"
    if target.exists():
        print("Existing .env preserved. Edit it locally to configure providers.")
        return
    target.write_text(
        "FT_AUTH_SIGNING_KEY=" + secrets.token_urlsafe(48) + "\nFT_ENABLE_DEV_LOGIN=false\n",
        encoding="utf-8",
    )
    (root / "var").mkdir(exist_ok=True)
    print("Generated local .env; key not displayed. Development login is disabled.")


if __name__ == "__main__":
    main()
