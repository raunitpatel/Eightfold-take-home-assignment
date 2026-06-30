"""
extractors/github_extractor.py — Extract from GitHub profile data.

This is an UNSTRUCTURED source per the problem statement (GitHub profile URL,
public REST/GraphQL API: name, bio, repos, languages). In a real production
system, this extractor would call the GitHub REST API live. For this
assignment we read a local JSON file shaped like a GitHub API response,
which keeps the extractor's *logic* identical to the live-API case — only
the I/O (file read vs HTTP GET) would change. This is an intentional and
explicitly noted scope decision (see README "assumptions").


Key extraction logic:
    - bio: freeform text — we scan for known role/seniority signals (light heuristic)
    - top_languages + repo topics → skills (inferred, lower confidence than
        a recruiter-asserted skill)
    - location: freeform string "City, Region" or "City, Country" — split heuristically
"""

import json
import logging
from typing import List, Optional

from .base import BaseExtractor
from .csv_extractor import _make_candidate_id
from ..schema import (
    CanonicalProfile, Location, Links, Skill, ProvenanceEntry
)
from ..normalizers import normalize_url, normalize_country, canonicalize_skill

logger = logging.getLogger(__name__)


def _split_location(raw: str):
    """
    Heuristic split of a freeform location string like:
        "Guwahati, India" → city="Guwahati", country="India"
        "San Francisco, CA, USA" → city="San Francisco", region="CA", country="USA"
        "Remote" → nothing usable
    
    WHY heuristic and not a geocoding API?
        No network access is guaranteed in this environment, and the problem
        doesn't require geocoding precision — just city/region/country buckets.
        A wrong split (e.g. only 1 part) degrades to "city only", which is
        acceptable per the "robust — never crash, never invent" constraint.
    """
    if not raw:
        return None, None, None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) == 0:
        return None, None, None
    if len(parts) == 1:
        return parts[0], None, None
    if len(parts) == 2:
        return parts[0], None, parts[1]
    # 3+ parts: city, region, country (take first and last two)
    return parts[0], parts[1], parts[-1]


class GitHubExtractor(BaseExtractor):

    @property
    def source_name(self) -> str:
        return "github_api"

    def extract(self, raw_data: str) -> List[CanonicalProfile]:
        """raw_data: path to JSON file shaped like GitHub API responses."""
        profiles = []
        
        try:
            with open(raw_data, encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            logger.error(f"GitHub data file not found: {raw_data}")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"GitHub JSON malformed: {e}")
            return []
        
        records = data.get("github_profiles") if isinstance(data, dict) else data
        if not isinstance(records, list):
            logger.error("GitHub JSON: unexpected structure")
            return []
        
        for i, rec in enumerate(records):
            try:
                profile = self._extract_profile(rec)
                if profile:
                    profiles.append(profile)
            except Exception as e:
                logger.warning(f"GitHub profile {i}: extraction failed: {e} — skipping")
        
        return profiles

    def _extract_profile(self, rec: dict) -> Optional[CanonicalProfile]:
        if not isinstance(rec, dict):
            return None
        
        name_raw = (rec.get("name") or "").strip()
        login = (rec.get("login") or "").strip()
        
        if not name_raw and not login:
            return None
        
        email_raw = (rec.get("email") or "").strip()
        from ..normalizers import normalize_email
        email = normalize_email(email_raw) if email_raw else None
        
        candidate_id = _make_candidate_id(
            name_raw or login,
            email or login
        )
        
        loc_raw = (rec.get("location") or "").strip()
        city, region, country_text = _split_location(loc_raw)
        country = normalize_country(country_text) if country_text else None
        location = Location(city=city, region=region, country=country) \
            if (city or region or country) else None
        
        github_url = normalize_url(rec.get("html_url") or f"github.com/{login}")
        portfolio = normalize_url(rec.get("blog") or "")
        links = Links(github=github_url, portfolio=portfolio)
        
        bio = (rec.get("bio") or "").strip() or None
        headline = bio  # bio is the closest thing GitHub has to a headline
        
        # ── Skills — INFERRED from top_languages + pinned repo topics ────
        # WHY lower confidence (0.5) than ATS-asserted skills (0.7)?
        #   A repo being written in Go means the person CAN write Go, but
        #   doesn't confirm professional proficiency the way a recruiter
        #   field or resume bullet does. This is an inference, not an
        #   assertion, so it should carry less weight in confidence scoring
        #   and in the merge step if it conflicts with an asserted skill.
        skills = []
        seen_skill_names = set()
        
        for lang in (rec.get("top_languages") or []):
            canon = canonicalize_skill(lang)
            key = canon.lower()
            if key not in seen_skill_names:
                seen_skill_names.add(key)
                skills.append(Skill(name=canon, confidence=0.5, sources=[self.source_name]))
        
        for repo in (rec.get("pinned_repos") or []):
            if not isinstance(repo, dict):
                continue
            for topic in (repo.get("topics") or []):
                canon = canonicalize_skill(topic)
                key = canon.lower()
                if key not in seen_skill_names:
                    seen_skill_names.add(key)
                    skills.append(Skill(name=canon, confidence=0.4, sources=[self.source_name]))
        
        provenance = []
        if name_raw:
            provenance.append(ProvenanceEntry(
                field_name="full_name", source=self.source_name,
                method="api_field:name", raw_value=name_raw
            ))
        if loc_raw:
            provenance.append(ProvenanceEntry(
                field_name="location", source=self.source_name,
                method="api_field:location + heuristic_split", raw_value=loc_raw
            ))
        if bio:
            provenance.append(ProvenanceEntry(
                field_name="headline", source=self.source_name,
                method="api_field:bio", raw_value=bio
            ))
        if skills:
            provenance.append(ProvenanceEntry(
                field_name="skills", source=self.source_name,
                method="inferred_from:top_languages+repo_topics",
                raw_value=", ".join(s.name for s in skills)
            ))
        
        return CanonicalProfile(
            candidate_id=candidate_id,
            full_name=name_raw or login or None,
            emails=[email] if email else [],
            location=location,
            links=links,
            headline=headline,
            skills=skills,
            provenance=provenance,
        )
