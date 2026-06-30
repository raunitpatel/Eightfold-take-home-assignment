"""
extractors/csv_extractor.py — Extract candidate data from recruiter CSV exports.

This is a STRUCTURED source: columns are known and fixed.
Fields: name, email, phone, current_company, title
"""

import csv
import hashlib
import logging
from typing import List

from .base import BaseExtractor
from ..schema import (
    CanonicalProfile, Links, ProvenanceEntry
)
from ..normalizers import normalize_phone, normalize_email, normalize_url

logger = logging.getLogger(__name__)


def _make_candidate_id(name: str, email: str) -> str:
    """
    Generate a stable candidate ID from name + email.
    
    WHY hash?
        We need an ID that is:
            1. Stable across runs (same input → same ID)
            2. Consistent across sources (CSV and ATS must produce the same ID
            for the same person so the merger can match them)
        
        Hashing name+email gives us this. We use the first 12 hex chars — 
        48 bits of entropy — which is plenty for thousands of candidates.
        
    Alternative considered: UUID v4 (random) — rejected because it's not
    stable across runs, so you can't merge across source files.
    
    Alternative considered: email alone — better, but name+email is more
    robust if the same person has multiple emails.
    """
    raw = f"{name.strip().lower()}|{email.strip().lower()}"
    return "cand_" + hashlib.md5(raw.encode()).hexdigest()[:12]


class CSVExtractor(BaseExtractor):

    @property
    def source_name(self) -> str:
        return "recruiter_csv"

    def extract(self, raw_data: str) -> List[CanonicalProfile]:
        """
        raw_data: path to CSV file.
        Returns one CanonicalProfile per non-header row.
        """
        profiles = []
        
        try:
            with open(raw_data, newline='', encoding='utf-8-sig') as f:
                # WHY DictReader? Gives us column-name access regardless of order.
                reader = csv.DictReader(f)
                
                for row_num, row in enumerate(reader, start=2):  # 2 = first data row
                    try:
                        profile = self._extract_row(row, row_num)
                        if profile:
                            profiles.append(profile)
                    except Exception as e:
                        # WHY catch per-row? One bad row must not kill the whole file.
                        logger.warning(f"CSV row {row_num} failed: {e} — skipping")
                        
        except FileNotFoundError:
            logger.error(f"CSV file not found: {raw_data}")
        except Exception as e:
            logger.error(f"CSV extraction failed: {e}")
        
        return profiles

    def _extract_row(self, row: dict, row_num: int) -> CanonicalProfile:
        """Extract one CSV row into a partial CanonicalProfile."""
        
        name_raw = (
            row.get("name") or row.get("full_name") or row.get("candidate_name") or ""
        ).strip()
        
        email_raw = (row.get("email") or row.get("email_address") or "").strip()
        email = normalize_email(email_raw)
        
        if not name_raw and not email:
            logger.debug(f"CSV row {row_num}: no name or email, skipping")
            return None
        
        candidate_id = _make_candidate_id(
            name_raw or "unknown",
            email or f"row_{row_num}"
        )
        
        phone_raw = (row.get("phone") or row.get("phone_number") or "").strip()
        phones = []
        if phone_raw:
            normed = normalize_phone(phone_raw)
            if normed:
                phones.append(normed)
        
        company = (row.get("current_company") or row.get("company") or "").strip()
        title = (row.get("title") or row.get("job_title") or "").strip()
        
        from ..schema import Experience
        experience = []
        if company or title:
            experience.append(Experience(
                company=company or "Unknown",
                title=title or "Unknown",
                start=None,  # CSV doesn't have dates
                end=None,
                summary=None,
            ))
        
        headline = None
        if title and company:
            headline = f"{title} at {company}"
        elif title:
            headline = title
        
        linkedin_raw = (row.get("linkedin") or row.get("linkedin_url") or "").strip()
        github_raw = (row.get("github") or row.get("github_url") or "").strip()
        
        links = Links(
            linkedin=normalize_url(linkedin_raw) if linkedin_raw else None,
            github=normalize_url(github_raw) if github_raw else None,
        )
        
        provenance = []
        if name_raw:
            provenance.append(ProvenanceEntry(
                field_name="full_name", source=self.source_name,
                method="direct_column_mapping", raw_value=name_raw
            ))
        if email:
            provenance.append(ProvenanceEntry(
                field_name="emails", source=self.source_name,
                method="direct_column_mapping", raw_value=email_raw
            ))
        if phones:
            provenance.append(ProvenanceEntry(
                field_name="phones", source=self.source_name,
                method="direct_column_mapping + E164_normalization",
                raw_value=phone_raw
            ))
        if title or company:
            provenance.append(ProvenanceEntry(
                field_name="experience", source=self.source_name,
                method="direct_column_mapping",
                raw_value=f"{title} @ {company}"
            ))
        
        return CanonicalProfile(
            candidate_id=candidate_id,
            full_name=name_raw if name_raw else None,
            emails=[email] if email else [],
            phones=phones,
            links=links,
            headline=headline,
            experience=experience,
            provenance=provenance,
        )
