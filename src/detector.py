"""
detector.py — Stage 1 of the pipeline: detect what kind of source a given
input file is, so the orchestrator knows which extractor to dispatch to.
"""

import json
import logging
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SourceType(Enum):
    RECRUITER_CSV = "recruiter_csv"
    ATS_JSON = "ats_json"
    GITHUB_API = "github_api"
    RECRUITER_NOTES = "recruiter_notes"
    UNKNOWN = "unknown"


def detect_source_type(path: str) -> SourceType:
    """
    Inspect a file path (extension + light content sniff) and return its
    SourceType. Never raises — returns UNKNOWN on any failure.
    """
    p = Path(path)

    if not p.exists():
        logger.error(f"detect_source_type: file does not exist: {path}")
        return SourceType.UNKNOWN

    suffix = p.suffix.lower()

    try:
        if suffix == ".csv":
            return SourceType.RECRUITER_CSV

        if suffix == ".txt":
            return SourceType.RECRUITER_NOTES

        if suffix == ".json":
            return _sniff_json_subtype(p)

        logger.warning(f"detect_source_type: unrecognized extension {suffix!r} for {path}")
        return SourceType.UNKNOWN

    except Exception as e:
        logger.error(f"detect_source_type: failed to inspect {path}: {e}")
        return SourceType.UNKNOWN


def _sniff_json_subtype(p: Path) -> SourceType:
    """
    Both ATS exports and GitHub API dumps are .json — we have to look at
    the actual structure to tell them apart.
    """
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error(f"_sniff_json_subtype: malformed JSON in {p}: {e}")
        return SourceType.UNKNOWN

    # Normalize to "the thing we'd inspect keys on"
    sample = None
    if isinstance(data, dict):
        if "applicants" in data or "candidates" in data:
            return SourceType.ATS_JSON
        if "github_profiles" in data:
            return SourceType.GITHUB_API
        sample = data
    elif isinstance(data, list) and data:
        sample = data[0]

    if isinstance(sample, dict):
        github_signals = {"login", "html_url", "public_repos", "bio"}
        ats_signals = {"applicant_id", "applicant_name", "contact_email", "work_history"}

        if github_signals & sample.keys():
            return SourceType.GITHUB_API
        if ats_signals & sample.keys():
            return SourceType.ATS_JSON

    logger.warning(f"_sniff_json_subtype: could not classify JSON structure in {p}")
    return SourceType.UNKNOWN
