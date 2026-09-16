from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.evaluation.sample_video_benchmark import write_sample_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create an evidence-backed benchmark from supplied demo videos"
    )
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(write_sample_benchmark(args.samples, args.output), indent=2))


if __name__ == "__main__":
    main()
