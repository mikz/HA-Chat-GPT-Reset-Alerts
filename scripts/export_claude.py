"""Export the standalone Claude HACS repository from the tested source tree."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def export(destination: Path) -> None:
    component = ROOT / "custom_components" / "claude_usage"
    files = [p for p in component.rglob("*") if p.is_file() and p.suffix in (".py", ".json")]
    files += [ROOT / p for p in ("LICENSE", "NOTICE.md", ".gitignore", "pyproject.toml",
                                ".github/workflows/validate.yml", "scripts/build_release.py",
                                "tests/conftest.py", "tests/test_models.py", "tests/test_reset_logic.py",
                                "tests/test_metadata.py")]
    files += list((ROOT / "tests").glob("test_claude_*.py"))
    for source in files:
        target = destination / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copyfile(ROOT / "docs/claude-usage.md", destination / "README.md")
    (destination / "hacs.json").write_text(json.dumps({
        "name": "Claude Usage (mikz)", "render_readme": True,
        "zip_release": True, "filename": "claude_usage.zip",
    }, indent=2) + "\n")
    version = json.loads((component / "manifest.json").read_text())["version"]
    project = (destination / "pyproject.toml").read_text()
    source_version = project.split('version = "')[1].split('"')[0]
    (destination / "pyproject.toml").write_text(project.replace('name = "ha-ai-usage"', 'name = "ha-claude-usage"')
                                               .replace(f'version = "{source_version}"', f'version = "{version}"'))
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    (destination / "SOURCE.json").write_text(json.dumps({
        "repository": "https://github.com/mikz/HA-Chat-GPT-Reset-Alerts",
        "commit": commit, "component": "custom_components/claude_usage",
    }, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    export(parser.parse_args().destination.resolve())
