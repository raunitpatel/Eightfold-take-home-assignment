#!/usr/bin/env python3
"""
cli.py — Thin command-line interface for the candidate profile transformer.

USAGE:
    python -m src.cli run \\
        --input sample_inputs/recruiter_export.csv \\
                sample_inputs/ats_blob.json \\
                sample_inputs/github_profiles.json \\
                sample_inputs/recruiter_notes.txt \\
        --output output/profiles.json

    python -m src.cli run --input ... --config custom_config.json --output output/custom.json

    python -m src.cli run --input ... --output output/profiles.json --pretty --summary
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from .pipeline import run_pipeline


def main():
    parser = argparse.ArgumentParser(
        prog="profile-transformer",
        description="Transform messy multi-source candidate data into a canonical profile JSON."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the pipeline end-to-end")
    run_parser.add_argument(
        "--input", "-i", nargs="+", required=True,
        help="One or more input file paths (CSV, ATS JSON, GitHub JSON, recruiter notes TXT)"
    )
    run_parser.add_argument(
        "--output", "-o", default="output/profiles.json",
        help="Output JSON file path (default: output/profiles.json)"
    )
    run_parser.add_argument(
        "--config", "-c", default=None,
        help="Path to a custom projection config JSON file. If omitted, uses the default schema."
    )
    run_parser.add_argument(
        "--pretty", action="store_true",
        help="Pretty-print the output JSON with indentation"
    )
    run_parser.add_argument(
        "--summary", action="store_true",
        help="Print a run summary (sources processed/skipped, validation failures) to stderr"
    )
    run_parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug-level logging"
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )

    if args.command == "run":
        _run_command(args)


def _run_command(args):
    config = None
    if args.config:
        try:
            with open(args.config, encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"ERROR: could not load config file {args.config}: {e}", file=sys.stderr)
            sys.exit(1)

    result = run_pipeline(args.input, config=config)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        if args.pretty:
            json.dump(result.projected_output, f, indent=2, ensure_ascii=False)
        else:
            json.dump(result.projected_output, f, ensure_ascii=False)

    print(f"Wrote {len(result.projected_output)} candidate profile(s) to {output_path}", file=sys.stderr)

    if args.summary:
        print(json.dumps(result.to_summary_dict(), indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
