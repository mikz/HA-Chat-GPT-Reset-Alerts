"""Build the ChatGPT-only HACS release archive."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components" / "chatgpt_usage"
TARGET = ROOT / "dist" / "chatgpt_usage.zip"


def build() -> Path:
    TARGET.parent.mkdir(exist_ok=True)
    with ZipFile(TARGET, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(SOURCE.rglob("*")):
            if path.is_file() and path.suffix in (".py", ".json", ".yaml", ".png", ".svg"):
                archive.write(path, path.relative_to(SOURCE))
    return TARGET


if __name__ == "__main__":
    print(build())
