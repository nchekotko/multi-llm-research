"""Archive only committed source; runtime files can never enter the release."""
import hashlib
import json
import subprocess
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    destination = root / "dist"
    destination.mkdir(exist_ok=True)
    archive = destination / "multi-llm-core-v0.1.0.zip"
    subprocess.run(["git", "archive", "--format=zip", "--prefix=multi-llm/",
                    "-o", str(archive), "HEAD"], cwd=root, check=True)
    print(json.dumps({"archive": str(archive),
                      "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}, indent=2))


if __name__ == "__main__":
    main()
