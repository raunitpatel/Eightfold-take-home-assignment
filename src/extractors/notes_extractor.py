"""
extractors/notes_extractor.py — Extract from free-text recruiter notes (.txt).

This is the HARDEST source to extract from because there's no structure
at all — just prose. We use regex heuristics anchored on labeled lines
(e.g. "Phone: ...", "Email: ...") because recruiters in practice tend to
write notes with some loose label:value structure even in "free text" —
this is a realistic assumption for this domain.

WHY regex and not an LLM call here?
    1. Determinism requirement: "same inputs produce the same output."
        An LLM call is non-deterministic (even at temp=0, model updates can
        shift outputs) and adds an external dependency/cost/latency.
    2. The labeled-line pattern in recruiter notes is common enough that
        regex captures the high-value fields (phone, email, links) reliably.
    3. For fields that are NOT label:value (skills, narrative experience),
        we use looser heuristics (keyword scanning against our skill
        dictionary, splitting on "---" for multi-candidate files) and accept
        this is best-effort — which is appropriate for an unstructured source
        and is the kind of judgment call we note as a scope decision in the README.

Multi-candidate handling: notes files may contain several candidates
separated by "---" or blank-line+"Candidate:" markers. We split on these.
"""

import re
import logging
from typing import List, Optional

from .base import BaseExtractor
from .csv_extractor import _make_candidate_id
from ..schema import CanonicalProfile, Location, Links, Skill, ProvenanceEntry
from ..normalizers import (
    normalize_phone, normalize_email, normalize_url,
    canonicalize_skill, SKILL_CANONICAL
)

logger = logging.getLogger(__name__)

LABEL_PATTERNS = {
    "phone": re.compile(r'(?:Phone|Mobile|Tel)\s*:\s*(.+)', re.IGNORECASE),
    "email": re.compile(r'Email\s*:\s*(\S+@\S+)', re.IGNORECASE),
    "linkedin": re.compile(r'LinkedIn\s*:\s*(\S+)', re.IGNORECASE),
    "github": re.compile(r'GitHub\s*:\s*(\S+)', re.IGNORECASE),
    "location": re.compile(r'Location\s*:\s*(.+)', re.IGNORECASE),
    "skills_line": re.compile(r'Skills(?:\s+noted)?\s*:\s*(.+)', re.IGNORECASE),
    "candidate_name": re.compile(r'Candidate\s*:\s*(.+)', re.IGNORECASE),
}


class NotesExtractor(BaseExtractor):

    @property
    def source_name(self) -> str:
        return "recruiter_notes"

    def extract(self, raw_data: str) -> List[CanonicalProfile]:
        """raw_data: path to .txt file. May contain multiple candidates."""
        try:
            with open(raw_data, encoding='utf-8') as f:
                content = f.read()
        except FileNotFoundError:
            logger.error(f"Notes file not found: {raw_data}")
            return []
        except Exception as e:
            logger.error(f"Notes file read failed: {e}")
            return []
        
        if not content.strip():
            logger.warning("Notes file is empty")
            return []
        
        # Split into per-candidate blocks on "---" separator lines.
        # WHY this separator? It's our own sample format; in production
        # this would be configurable or detected via blank-line runs.
        blocks = re.split(r'\n-{3,}\n', content)
        
        profiles = []
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            try:
                profile = self._extract_block(block)
                if profile:
                    profiles.append(profile)
            except Exception as e:
                logger.warning(f"Notes block failed: {e} — skipping")
        
        return profiles

    def _extract_block(self, block: str) -> Optional[CanonicalProfile]:
        provenance = []
        
        # ── Name ──────────────────────────────────────────────────────────
        name_match = LABEL_PATTERNS["candidate_name"].search(block)
        name_raw = name_match.group(1).strip() if name_match else None
        if name_match:
            provenance.append(ProvenanceEntry(
                field_name="full_name", source=self.source_name,
                method="regex_label_match:Candidate", raw_value=name_raw
            ))
        
        email_match = LABEL_PATTERNS["email"].search(block)
        email_raw = email_match.group(1).strip() if email_match else None
        email = normalize_email(email_raw) if email_raw else None
        if email:
            provenance.append(ProvenanceEntry(
                field_name="emails", source=self.source_name,
                method="regex_label_match:Email", raw_value=email_raw
            ))
        
        if not name_raw and not email:
            logger.debug("Notes block: no name or email found, skipping")
            return None
        
        candidate_id = _make_candidate_id(name_raw or "unknown", email or "unknown")
        
        phone_match = LABEL_PATTERNS["phone"].search(block)
        phones = []
        if phone_match:
            phone_raw = phone_match.group(1).strip()
            normed = normalize_phone(phone_raw)
            if normed:
                phones.append(normed)
                provenance.append(ProvenanceEntry(
                    field_name="phones", source=self.source_name,
                    method="regex_label_match:Phone + E164_normalization",
                    raw_value=phone_raw
                ))
        
        li_match = LABEL_PATTERNS["linkedin"].search(block)
        gh_match = LABEL_PATTERNS["github"].search(block)
        linkedin = normalize_url(li_match.group(1)) if li_match else None
        github = normalize_url(gh_match.group(1)) if gh_match else None
        links = Links(linkedin=linkedin, github=github) if (linkedin or github) else None
        if linkedin or github:
            provenance.append(ProvenanceEntry(
                field_name="links", source=self.source_name,
                method="regex_label_match:LinkedIn/GitHub",
                raw_value=f"li:{linkedin} gh:{github}"
            ))
        
        loc_match = LABEL_PATTERNS["location"].search(block)
        location = None
        if loc_match:
            loc_raw = loc_match.group(1).strip()
            parts = [p.strip() for p in loc_raw.split(",") if p.strip()]
            from ..normalizers import normalize_country
            city = parts[0] if len(parts) >= 1 else None
            region = parts[1] if len(parts) >= 2 else None
            country = normalize_country(parts[-1]) if len(parts) >= 1 else None
            location = Location(city=city, region=region, country=country)
            provenance.append(ProvenanceEntry(
                field_name="location", source=self.source_name,
                method="regex_label_match:Location", raw_value=loc_raw
            ))
        
        # ── Skills — explicit "Skills noted:" line + dictionary keyword scan ─
        # WHY both?
        #   The labeled line ("Skills noted: Python, FastAPI, ...") is high-
        #   confidence — the recruiter deliberately listed it.
        #   We ALSO scan the rest of the prose for any of our known skill
        #   keywords as a lower-confidence supplementary signal (e.g. "Go and
        #   Rust as languages he's learning" — these aren't in the labeled
        #   line but are real signal worth capturing at lower confidence).
        skills = []
        seen = set()
        
        skills_match = LABEL_PATTERNS["skills_line"].search(block)
        if skills_match:
            raw_line = skills_match.group(1)
            # Stop at first period to avoid swallowing the next sentence
            raw_line = raw_line.split(".")[0]
            for s in raw_line.split(","):
                s = s.strip()
                if s:
                    canon = canonicalize_skill(s)
                    key = canon.lower()
                    if key not in seen:
                        seen.add(key)
                        skills.append(Skill(name=canon, confidence=0.65, sources=[self.source_name]))
            provenance.append(ProvenanceEntry(
                field_name="skills", source=self.source_name,
                method="regex_label_match:Skills_noted", raw_value=raw_line.strip()
            ))
        
        # Supplementary keyword scan over the whole block (lower confidence)
        block_lower = block.lower()
        for alias, canonical in SKILL_CANONICAL.items():
            key = canonical.lower()
            if key in seen:
                continue
            # word-boundary match to avoid partial substring hits
            if re.search(r'\b' + re.escape(alias) + r'\b', block_lower):
                seen.add(key)
                skills.append(Skill(name=canonical, confidence=0.35, sources=[self.source_name]))
        
        # ── Headline: best-effort from first sentence after name ──────────
        headline = None
        headline_match = re.search(
            r'(?:Strong background in|Background:)\s*(.+?)\.', block
        )
        if headline_match:
            headline = headline_match.group(1).strip()
        
        return CanonicalProfile(
            candidate_id=candidate_id,
            full_name=name_raw,
            emails=[email] if email else [],
            phones=phones,
            location=location,
            links=links,
            headline=headline,
            skills=skills,
            provenance=provenance,
        )
