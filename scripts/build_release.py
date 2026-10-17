"""Build one provider's HACS release archive."""

from pathlib import Path
import argparse
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def build(domain: str = "chatgpt_usage") -> Path:
    source = ROOT / "custom_components" / domain
    target = ROOT / "dist" / f"{domain}.zip"
    target.parent.mkdir(exist_ok=True)
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix in (".py", ".json", ".yaml", ".png", ".svg"):
                archive.write(path, path.relative_to(source))
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", choices=("chatgpt_usage", "claude_usage"), default="chatgpt_usage")
    print(build(parser.parse_args().domain))
