"""
extractors/base.py — Abstract base class for all source extractors.
"""

from abc import ABC, abstractmethod
from typing import Dict, List
from ..schema import CanonicalProfile


class BaseExtractor(ABC):
    """
    Contract: extract(raw_data) → list of partial CanonicalProfiles.
    
    Returns a LIST because a single source file (e.g. CSV, ATS JSON)
    can contain multiple candidates.
    
    source_name: human-readable identifier used in provenance records.
      e.g. "recruiter_csv", "ats_json", "github_api", "recruiter_notes"
    """

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Identifier used in provenance tracking."""
        ...

    @abstractmethod
    def extract(self, raw_data) -> List[CanonicalProfile]:
        """
        Parse raw_data and return partial canonical profiles.
        
        raw_data type varies by extractor:
          - CSV extractor: path to CSV file
          - JSON extractor: path to JSON file
          - TXT extractor: path to text file
        
        MUST NOT raise on bad data — return empty list or partial profile.
        Robustness requirement: missing/garbage source must not crash the run.
        """
        ...
