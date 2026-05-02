"""Freeze baseline artifact checksums for reproducibility gates."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path


DEFAULT_ARTIFACTS = [
    "baseline_real_sec_fy2023.json",
    "baseline_real_sec_fy2023.pipeline.json",
    "financebench_baseline.json",
    "financebench_baseline.pipeline.json",
    "financebench_baseline.patronus.json",
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze baseline checksum manifest")
    parser.add_argument("--dir", default="tests/evaluation/eval_results", help="Artifact directory")
    parser.add_argument("--output", default="baseline_manifest.json", help="Output manifest filename")
    args = parser.parse_args()

    out_dir = Path(args.dir)
    manifest = {
        "created_at": str(date.today()),
        "purpose": "frozen baselines for co-primary benchmark gates",
        "artifacts": [],
    }

    for rel in DEFAULT_ARTIFACTS:
        p = out_dir / rel
        if not p.exists():
            continue
        manifest["artifacts"].append(
            {
                "path": str(p),
                "sha256": sha256(p),
                "size_bytes": p.stat().st_size,
            }
        )

    out_path = out_dir / args.output
    out_path.write_text(json.dumps(manifest, indent=2))
    print(f"Wrote {out_path} with {len(manifest['artifacts'])} artifacts")


if __name__ == "__main__":
    main()

