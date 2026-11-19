"""Check Gemini 4 Hydrogen against the production nutrition schema."""
import argparse
import base64
import os
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jpeg", type=Path)
    args = parser.parse_args()
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        parser.exit(2, "Set GEMINI_API_KEY in the environment.\n")
    photo = args.jpeg.read_bytes()
    if not photo.startswith(b"\xff\xd8\xff") or len(photo) > 1_000_000:
        parser.error("Use a JPEG up to 1 MB.")
    with TemporaryDirectory() as temp:
        os.environ["DATABASE_PATH"] = str(Path(temp) / "smoke.sqlite3")
        import index as bot

        client = bot._get_llm_client(key, "https://generativelanguage.googleapis.com/v1beta/openai/")
        image_url = "data:image/jpeg;base64," + base64.b64encode(photo).decode("ascii")
        message = [
            {"type": "text", "text": "Оцени блюда и порции на фото."},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
        try:
            raw = bot._request_completion(message, structured=True, client=client,
                                          model="gemini-4-hydrogen")
            data = bot._parse_ai_result(raw)
        except Exception as error:
            status = getattr(error, "status_code", None)
            parser.exit(1, f"Hydrogen check failed: {type(error).__name__}, HTTP {status or 'n/a'}\n")
        if not data["items"]:
            parser.exit(1, "Hydrogen returned valid JSON with no recognized foods.\n")
        print(f"Hydrogen returned valid nutrition JSON ({len(data['items'])} products).")


if __name__ == "__main__":
    main()
