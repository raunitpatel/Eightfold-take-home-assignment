"""
pipeline.py — Orchestrates the full pipeline:
detect -> load -> extract -> merge -> confidence -> project -> validate
"""

import logging
from pathlib import Path
from typing import List, Dict, Optional, Any

from .detector import detect_source_type, SourceType
from .extractors.csv_extractor import CSVExtractor
from .extractors.ats_extractor import ATSExtractor
from .extractors.github_extractor import GitHubExtractor
from .extractors.notes_extractor import NotesExtractor
from .merger import merge_all
from .projector import project_all
from .validator import validate_all
from .schema import CanonicalProfile

logger = logging.getLogger(__name__)

EXTRACTOR_REGISTRY = {
    SourceType.RECRUITER_CSV: CSVExtractor(),
    SourceType.ATS_JSON: ATSExtractor(),
    SourceType.GITHUB_API: GitHubExtractor(),
    SourceType.RECRUITER_NOTES: NotesExtractor(),
}


class PipelineResult:
    """
    Bundles everything a caller (CLI/UI) might want after a run:
    the final canonical profiles, the projected output, and validation
    results — plus bookkeeping on which sources failed/were skipped, for
    transparency (the "explainable" requirement extends to the run itself,
    not just individual field values).
    """

    def __init__(self):
        self.canonical_profiles: List[CanonicalProfile] = []
        self.projected_output: List[Dict[str, Any]] = []
        self.validation_results: List[Any] = []
        self.skipped_sources: List[str] = []
        self.source_summary: Dict[str, int] = {}  # source_name -> partial profile count

    def to_summary_dict(self) -> Dict[str, Any]:
        return {
            "candidates_found": len(self.canonical_profiles),
            "sources_processed": self.source_summary,
            "sources_skipped": self.skipped_sources,
            "validation_failures": sum(1 for v in self.validation_results if not v.is_valid),
        }


def run_pipeline(
    input_paths: List[str],
    config: Optional[Dict] = None,
) -> PipelineResult:
    """
    Run the full pipeline over a list of input file paths.

    Stages, in order:
        1. detect   — classify each path's source type
        2. extract  — run the matching extractor, collect partial profiles
        3. merge    — entity-resolve + field-merge + score confidence
        4. project  — reshape per the config (or DEFAULT_CONFIG)
        5. validate — check projected output against the config's schema

    """
    result = PipelineResult()
    all_partial_profiles: List[CanonicalProfile] = []

    # ── Stage 1 + 2: detect & extract, per file ─────────────────────────
    for path in input_paths:
        if not Path(path).exists():
            logger.error(f"Input path does not exist, skipping: {path}")
            result.skipped_sources.append(path)
            continue

        source_type = detect_source_type(path)

        if source_type == SourceType.UNKNOWN:
            logger.error(f"Could not determine source type, skipping: {path}")
            result.skipped_sources.append(path)
            continue

        extractor = EXTRACTOR_REGISTRY.get(source_type)
        if extractor is None:
            logger.error(f"No extractor registered for {source_type}, skipping: {path}")
            result.skipped_sources.append(path)
            continue

        try:
            partials = extractor.extract(path)
        except Exception as e:
            logger.error(f"Extractor {extractor.source_name} crashed on {path}: {e}")
            result.skipped_sources.append(path)
            continue

        logger.info(f"{extractor.source_name}: extracted {len(partials)} partial profile(s) from {path}")
        result.source_summary[extractor.source_name] = \
            result.source_summary.get(extractor.source_name, 0) + len(partials)
        all_partial_profiles.extend(partials)

    if not all_partial_profiles:
        logger.warning("No partial profiles extracted from any source — returning empty result")
        return result

    # ── Stage 3: merge (entity resolution + field merge + confidence) ───
    merged_profiles = merge_all(all_partial_profiles)
    result.canonical_profiles = merged_profiles

    # ── Stage 4: project ─────────────────────────────────────────────────
    projected = project_all(merged_profiles, config)
    result.projected_output = projected

    # ── Stage 5: validate ────────────────────────────────────────────────
    result.validation_results = validate_all(projected, config)

    return result
