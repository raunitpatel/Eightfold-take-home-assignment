"""
extractors/ats_extractor.py — Extract from ATS JSON blob.

This is also a STRUCTURED source, but the field names DON'T match ours.
This is the key challenge: we need a mapping layer.

ATS field → our canonical field:
    applicant_name → full_name
    contact_email  → emails[]
    mobile         → phones[]
    org            → experience[0].company (current)
    role           → experience[0].title (current)
    location_city/state/country → location
    portfolio_url  → links.portfolio
    linkedin       → links.linkedin
    github         → links.github
    skills_raw     → skills[] (comma-separated string)
    years_exp      → years_experience
    education[]    → education[]
    work_history[] → experience[]    
"""

import json
import logging
import hashlib
from typing import List, Optional

from .base import BaseExtractor
from .csv_extractor import _make_candidate_id
from ..schema import (
    CanonicalProfile, Location, Links, Skill,
    Experience, Education, ProvenanceEntry
)
from ..normalizers import (
    normalize_phone, normalize_email, normalize_url,
    normalize_country, normalize_date, canonicalize_skill
)

logger = logging.getLogger(__name__)


class ATSExtractor(BaseExtractor):

    @property
    def source_name(self) -> str:
        return "ats_json"

    def extract(self, raw_data: str) -> List[CanonicalProfile]:
        """raw_data: path to ATS JSON file."""
        profiles = []
        
        try:
            with open(raw_data, encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            logger.error(f"ATS JSON file not found: {raw_data}")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"ATS JSON malformed: {e}")
            return []
        
        # The ATS blob wraps records under an "applicants" key.
        # WHY check for this key gracefully?
        #   Different ATS vendors use different root keys: "candidates",
        #   "applicants", "records", "data". We handle the known case
        #   and fall back to treating the root as a list.
        if isinstance(data, dict):
            applicants = data.get("applicants") or data.get("candidates") or \
                        data.get("records") or []
        elif isinstance(data, list):
            applicants = data
        else:
            logger.error("ATS JSON: unexpected root structure")
            return []
        
        for i, applicant in enumerate(applicants):
            try:
                profile = self._extract_applicant(applicant)
                if profile:
                    profiles.append(profile)
            except Exception as e:
                logger.warning(f"ATS applicant {i}: extraction failed: {e} — skipping")
        
        return profiles

    def _extract_applicant(self, ap: dict) -> Optional[CanonicalProfile]:
        """Map one ATS applicant dict to a CanonicalProfile."""
        
        if not isinstance(ap, dict):
            return None
        
        name_raw = (ap.get("applicant_name") or ap.get("name") or "").strip()
        
        email_raw = (ap.get("contact_email") or ap.get("email") or "").strip()
        email = normalize_email(email_raw)
        
        if not name_raw and not email:
            return None
        
        candidate_id = _make_candidate_id(
            name_raw or "unknown",
            email or ap.get("applicant_id", "unknown")
        )
        
        phone_raw = str(ap.get("mobile") or ap.get("phone") or "").strip()
        phones = []
        if phone_raw and phone_raw != "0":
            normed = normalize_phone(phone_raw)
            if normed:
                phones.append(normed)
        
        city = (ap.get("location_city") or "").strip() or None
        region = (ap.get("location_state") or ap.get("location_region") or "").strip() or None
        country_raw = (ap.get("location_country") or "").strip()
        country = normalize_country(country_raw) if country_raw else None
        
        location = Location(city=city, region=region, country=country) \
            if (city or region or country) else None
        
        linkedin = normalize_url(ap.get("linkedin") or "")
        github = normalize_url(ap.get("github") or "")
        portfolio = normalize_url(ap.get("portfolio_url") or "")
        
        links = Links(linkedin=linkedin, github=github, portfolio=portfolio) \
            if (linkedin or github or portfolio) else None
        
        skills_raw_str = ap.get("skills_raw") or ""
        skills = []
        if skills_raw_str:
            for s in skills_raw_str.split(","):
                s = s.strip()
                if s:
                    skills.append(Skill(
                        name=canonicalize_skill(s),
                        confidence=0.7,
                        sources=[self.source_name]
                    ))
        
        years_exp = None
        raw_years = ap.get("years_exp")
        if raw_years is not None:
            try:
                years_exp = float(raw_years)
            except (ValueError, TypeError):
                pass
        
        education = []
        for edu in (ap.get("education") or []):
            if not isinstance(edu, dict):
                continue
            institution = (edu.get("inst") or edu.get("institution") or "").strip()
            if not institution:
                continue
            education.append(Education(
                institution=institution,
                degree=(edu.get("deg") or edu.get("degree") or "").strip() or None,
                field_of_study=(edu.get("field_of_study") or edu.get("field") or "").strip() or None,
                end_year=edu.get("grad_year") or edu.get("end_year"),
            ))
        
        experience = []
        for job in (ap.get("work_history") or []):
            if not isinstance(job, dict):
                continue
            company = (job.get("company") or "").strip()
            if not company:
                continue
            
            start_raw = job.get("from") or job.get("start")
            end_raw = job.get("to") or job.get("end")
            
            experience.append(Experience(
                company=company,
                title=(job.get("position") or job.get("title") or "").strip() or None,
                start=normalize_date(str(start_raw)) if start_raw else None,
                end=normalize_date(str(end_raw)) if end_raw else None,
                summary=(job.get("desc") or job.get("summary") or "").strip() or None,
            ))
        
        if not experience:
            org = (ap.get("org") or "").strip()
            role = (ap.get("role") or "").strip()
            if org or role:
                experience.append(Experience(
                    company=org or "Unknown",
                    title=role or "Unknown",
                    start=None, end=None, summary=None,
                ))
        
        headline = None
        if experience:
            e = experience[0]
            if e.title and e.company:
                headline = f"{e.title} at {e.company}"
            elif e.title:
                headline = e.title
        
        provenance = []
        field_source_map = {
            "full_name": (name_raw, "field_remapping: applicant_name→full_name"),
            "emails": (email_raw, "field_remapping: contact_email→emails"),
            "phones": (phone_raw, "field_remapping: mobile→phones + E164_normalization"),
            "location": (f"{city},{region},{country_raw}", "field_remapping: location_city/state/country"),
            "links": (f"li:{ap.get('linkedin')} gh:{ap.get('github')}", "field_remapping: linkedin/github"),
            "skills": (skills_raw_str, "field_remapping: skills_raw + split + canonicalize"),
            "years_experience": (str(raw_years), "field_remapping: years_exp"),
        }
        
        for field_name, (raw_val, method) in field_source_map.items():
            if raw_val and raw_val.strip("None ,"):
                provenance.append(ProvenanceEntry(
                    field_name=field_name,
                    source=self.source_name,
                    method=method,
                    raw_value=str(raw_val)[:200],
                ))
        
        return CanonicalProfile(
            candidate_id=candidate_id,
            full_name=name_raw or None,
            emails=[email] if email else [],
            phones=phones,
            location=location,
            links=links,
            headline=headline,
            years_experience=years_exp,
            skills=skills,
            experience=experience,
            education=education,
            provenance=provenance,
        )
