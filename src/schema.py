"""
schema.py — Canonical data models for the candidate profile.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any



@dataclass
class Location:
    """
    WHY separate city/region/country?
        Because downstream systems often filter by country (ISO code) or region.
        Storing as a flat string like "Guwahati, Assam, India" makes filtering hard.
    """
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None   # ISO-3166 alpha-2 e.g. "IN", "US"

    def to_dict(self) -> Dict[str, Any]:
        return {"city": self.city, "region": self.region, "country": self.country}


@dataclass
class Links:
    """
    Structured link bag. 'other' is a list because a candidate may have
    personal blog, portfolio, Kaggle, LeetCode — all valid, all "other".
    """
    linkedin: Optional[str] = None
    github: Optional[str] = None
    portfolio: Optional[str] = None
    other: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "linkedin": self.linkedin,
            "github": self.github,
            "portfolio": self.portfolio,
            "other": self.other,
        }


@dataclass
class Skill:
    """
    A skill mentioned in 3 sources (resume + CSV + GitHub) is more credible
    than one mentioned only in free-text recruiter notes.
    confidence: 0.0 – 1.0
    sources: which source types reported this skill
    """
    name: str                               # canonical name, e.g. "PostgreSQL" not "psql"
    confidence: float = 1.0
    sources: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "confidence": self.confidence, "sources": self.sources}


@dataclass
class Experience:
    """
    Dates stored as YYYY-MM strings (or None for current role).
    """
    company: str
    title: str
    start: Optional[str] = None    # "YYYY-MM"
    end: Optional[str] = None      # "YYYY-MM" or None if current
    summary: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "company": self.company,
            "title": self.title,
            "start": self.start,
            "end": self.end,
            "summary": self.summary,
        }


@dataclass
class Education:
    institution: str
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    end_year: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "institution": self.institution,
            "degree": self.degree,
            "field": self.field_of_study,
            "end_year": self.end_year,
        }


@dataclass
class ProvenanceEntry:
    """
    field: which canonical field this provenance entry covers
    source: source identifier e.g. "recruiter_csv", "ats_json", "github", "recruiter_notes"
    method: how the value was obtained e.g. "direct_mapping", "regex_extraction", "api_field"
    raw_value: what the source actually said (before normalization)
    """
    field_name: str
    source: str
    method: str
    raw_value: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field": self.field_name,
            "source": self.source,
            "method": self.method,
            "raw_value": self.raw_value,
        }



@dataclass
class CanonicalProfile:
    """
    This is the INTERNAL representation. It always has all fields.
    The output (shaped by config) is a different dict produced by the projector.

    WHY separate internal from output?
        - We can change the output schema without touching extraction/merge logic.
        - The projector can rename, subset, and reformat without data loss.
        - The validator checks the output, not this internal object.
    """
    candidate_id: str
    full_name: Optional[str] = None
    emails: List[str] = field(default_factory=list)
    phones: List[str] = field(default_factory=list)          # E.164 format
    location: Optional[Location] = None
    links: Optional[Links] = None
    headline: Optional[str] = None
    years_experience: Optional[float] = None
    skills: List[Skill] = field(default_factory=list)
    experience: List[Experience] = field(default_factory=list)
    education: List[Education] = field(default_factory=list)
    provenance: List[ProvenanceEntry] = field(default_factory=list)
    overall_confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the full canonical profile to a plain dict."""
        return {
            "candidate_id": self.candidate_id,
            "full_name": self.full_name,
            "emails": self.emails,
            "phones": self.phones,
            "location": self.location.to_dict() if self.location else None,
            "links": self.links.to_dict() if self.links else None,
            "headline": self.headline,
            "years_experience": self.years_experience,
            "skills": [s.to_dict() for s in self.skills],
            "experience": [e.to_dict() for e in self.experience],
            "education": [ed.to_dict() for ed in self.education],
            "provenance": [p.to_dict() for p in self.provenance],
            "overall_confidence": self.overall_confidence,
        }
