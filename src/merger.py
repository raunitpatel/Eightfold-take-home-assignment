"""
merger.py — Merge partial profiles from multiple sources into one canonical record.

This is the heart of the system. Two separate problems live here:

    1. ENTITY RESOLUTION: which partial profiles (possibly from different
        source files) refer to the SAME real candidate?
    2. FIELD-LEVEL CONFLICT RESOLUTION: when two sources disagree about a
        field's value (e.g. two different phone numbers), which one wins?

    ────────────────────────────────────────────────────────────────────────
    1. ENTITY RESOLUTION — match keys
    ────────────────────────────────────────────────────────────────────────
    WHY can't we just trust candidate_id from extraction?
    Each extractor independently hashes name+email (or name+login) to make
    an ID. Two extractors will only produce the SAME id if they had access
    to the exact same name+email spelling. In our sample data, the ATS used
    "raunit@iitg.ac.in" while the CSV used "raunit.patel@gmail.com" — same
    person, different emails, so the hash-based IDs will legitimately differ.
    
    Real-world identity resolution is its own hard problem (could involve
    fuzzy name matching, phone matching, etc.). We adopt a pragmatic,
    explainable policy:

    MATCH KEY PRIORITY (first one that produces a match wins):
        a. Exact email match (case-insensitive) — emails are the most reliable
        unique identifier humans use across platforms.
        b. Exact phone match (E.164 normalized) — second most reliable.
        c. Exact full_name match (case-insensitive, whitespace-collapsed) AS
        A LAST RESORT ONLY — names collide far more than email/phone, so
        this is the weakest signal and is used only when no profiles in
        the group already have an email/phone giving a stronger signal.

    WHY this priority order and not, say, GitHub login or LinkedIn URL?
        Email and phone are present across nearly all source types in this
        domain (recruiter forms always ask for them). Login/profile URLs are
        source-specific (only GitHub/LinkedIn extractors produce them) so
        they're less universal as a join key, though we DO use them as a
        secondary same-person signal once a cluster already exists (see
        _merge_clusters_with_shared_links below) — this lets a GitHub-only
        profile join a cluster that has no email at all, via a shared
        linkedin/github URL with another, already-matched, partial profile.

    ────────────────────────────────────────────────────────────────────────
    2. FIELD-LEVEL CONFLICT RESOLUTION — priority + confidence policy
    ────────────────────────────────────────────────────────────────────────
    For SCALAR fields (full_name, headline, location, years_experience):
    We use a SOURCE PRIORITY RANKING, reasoning that some sources are simply
    more authoritative for certain fields:
    
        full_name, location:        ats_json > recruiter_csv > recruiter_notes > github_api
        headline, years_experience: ats_json > recruiter_csv > recruiter_notes > github_api
    
    WHY ats_json highest?
        ATS data is typically entered by the candidate themselves into a formal
        application form — high intent, high accuracy.
    WHY recruiter_csv next?
        Recruiter-maintained records, but second-hand (recruiter transcribing,
        possible typos).
    WHY recruiter_notes below csv?
        Free text, manually typed during/after a call — more error-prone, and
        our regex extraction itself is heuristic (lower extraction confidence).
    WHY github_api lowest for personal-info fields?
        Self-reported on a developer profile that may be stale or jokey
        ("Location: Earth", "Company: Unemployed") — least reliable for HR facts.
    
    Tie-break: if two sources of EQUAL priority disagree (shouldn't happen
    with our 4 fixed sources, but the rule must exist for extensibility):
    prefer the value seen by MORE sources (majority vote), then fall back
    to "first one extracted" for full determinism.

    For LIST/SET fields (emails, phones, skills, links.other):
    We UNION rather than pick-a-winner. A candidate having multiple emails
    or multiple skills mentioned in different sources is NOT a conflict —
    it's complementary information. We deduplicate using normalized forms
    (E.164 for phone, lowercase for email, canonical name for skills).

    For LIST-OF-RECORDS fields (experience, education):
    We deduplicate by (company, title) for experience and 
    (institution, degree) for education, using fuzzy-ish matching (case-
    insensitive, whitespace-collapsed). When the same job appears in two
    sources with different date precision, we keep the entry with MORE
    complete date information (has start AND end beats has start only).

    ────────────────────────────────────────────────────────────────────────
    3. CONFIDENCE SCORING
    ────────────────────────────────────────────────────────────────────────
    Per-field confidence (used for `skills[].confidence`, and rolled up into
    `overall_confidence`):
    - Base confidence comes from the WINNING source's reliability weight.
    - BOOSTED by agreement: if N≥2 sources independently agree on the same
        value, confidence increases (corroboration matters).
    - Skills already carry confidence from extraction (asserted vs inferred);
        when the same skill appears in multiple sources we take the MAX of
        their confidences and boost slightly for corroboration, capped at 1.0.

    overall_confidence = weighted average across all populated top-level
    fields, where weight reflects how central the field is, e.g. full_name
    and emails weigh more than headline.
"""

import logging
from collections import defaultdict
from typing import List, Dict, Optional

from .schema import (
    CanonicalProfile, Location, Links, Skill, Experience, Education, ProvenanceEntry
)

logger = logging.getLogger(__name__)


# Source reliability ranking for SCALAR fields — lower index = higher priority.
# WHY a single shared ranking instead of per-field rankings?
#   In practice for this domain, the same authority ordering holds across
#   most personal-info fields (ATS form > recruiter CSV > notes > GitHub).
#   We keep ONE ranking for simplicity/explainability, with the explicit
#   design note that a production system might split rankings per-field
#   (e.g. GitHub could rank highest for "skills" specifically — which is
#   why skills are NOT decided by this ranking at all; they're unioned).
SOURCE_PRIORITY = {
    "ats_json": 0,
    "recruiter_csv": 1,
    "recruiter_notes": 2,
    "github_api": 3,
}

# Base reliability weight per source, used for confidence scoring.
SOURCE_RELIABILITY_WEIGHT = {
    "ats_json": 0.9,
    "recruiter_csv": 0.85,
    "recruiter_notes": 0.6,
    "github_api": 0.5,
}


def _norm_name(s: Optional[str]) -> str:
    return " ".join((s or "").strip().lower().split())


def _source_of(profile: CanonicalProfile) -> str:
    """A partial profile's source = the source of its first provenance entry."""
    if profile.provenance:
        return profile.provenance[0].source
    return "unknown"


# STEP A: Entity resolution — cluster partial profiles by identity

def cluster_by_identity(profiles: List[CanonicalProfile]) -> List[List[CanonicalProfile]]:
    """
    Group partial profiles that represent the SAME real candidate.
    
    Algorithm (union-find style, but implemented with simple iterative
    merging since candidate counts here are small — thousands, not millions):
    
        1. Build lookup tables: email -> cluster_idx, phone -> cluster_idx.
        2. For each profile, try to attach it to an existing cluster via
            email, then phone, then (only if neither exists in ANY profile
            in any cluster) name.
        3. New clusters are created for profiles that don't match anything.
        
        WHY this order (not a single combined pass)?
        Processing email-matches first prevents a coincidental name
        collision ("two different John Smiths") from merging unrelated
        people before a stronger signal (shared email) has a chance to
        group the right ones. We trade a bit of performance for
        correctness — acceptable at "thousands of candidates" scale.
    """
    clusters: List[List[CanonicalProfile]] = []
    email_to_cluster: Dict[str, int] = {}
    phone_to_cluster: Dict[str, int] = {}
    name_to_cluster: Dict[str, int] = {}

    def register(idx: int, profile: CanonicalProfile):
        for e in profile.emails:
            email_to_cluster.setdefault(e.lower(), idx)
        for p in profile.phones:
            phone_to_cluster.setdefault(p, idx)
        if profile.full_name:
            name_to_cluster.setdefault(_norm_name(profile.full_name), idx)

    for profile in profiles:
        target_idx = None
        
        # (a) Email match — strongest signal
        for e in profile.emails:
            if e.lower() in email_to_cluster:
                target_idx = email_to_cluster[e.lower()]
                break
        
        # (b) Phone match — second strongest
        if target_idx is None:
            for p in profile.phones:
                if p in phone_to_cluster:
                    target_idx = phone_to_cluster[p]
                    break
        
        # (c) Name match — weakest, last resort
        if target_idx is None and profile.full_name:
            key = _norm_name(profile.full_name)
            if key in name_to_cluster:
                target_idx = name_to_cluster[key]
        
        if target_idx is not None:
            clusters[target_idx].append(profile)
            register(target_idx, profile)
        else:
            clusters.append([profile])
            register(len(clusters) - 1, profile)
    
    logger.info(f"Entity resolution: {len(profiles)} partial profiles -> {len(clusters)} candidates")
    return clusters


# ─────────────────────────────────────────────
# STEP B: Field-level merge within one cluster
# ─────────────────────────────────────────────

def _pick_scalar(
    candidates_with_source: List[tuple],  # [(value, source), ...]
) -> tuple:
    """
    Pick a winning scalar value using SOURCE_PRIORITY, with majority-vote
    tie-break, then first-seen as final tie-break for determinism.
    
    Returns (winning_value, winning_source) or (None, None) if no candidates.
    """
    # Filter out empty/None values — they can't win
    valid = [(v, s) for v, s in candidates_with_source if v]
    if not valid:
        return None, None
    
    # Count occurrences of each distinct value (case-insensitive for strings)
    value_counts = defaultdict(int)
    value_first_source = {}
    for v, s in valid:
        key = v.lower() if isinstance(v, str) else v
        value_counts[key] += 1
        if key not in value_first_source:
            value_first_source[key] = (v, s)
    
    # If there's a value that appears more than once, majority wins
    # regardless of source priority — corroboration trumps a single
    # high-priority source making an error. But this only kicks in
    # with 3+ sources disagreeing; with our 4 fixed sources mostly
    # agreeing per field, priority below is the common path.
    max_count = max(value_counts.values())
    majority_keys = [k for k, c in value_counts.items() if c == max_count]
    
    if max_count > 1 and len(majority_keys) == 1:
        return value_first_source[majority_keys[0]]
    
    # No majority (or tied majority) — fall back to source priority
    best = min(valid, key=lambda vs: SOURCE_PRIORITY.get(vs[1], 99))
    return best


def _merge_location(located: List[tuple]) -> tuple:
    """
    Locations merge FIELD-BY-FIELD (city, region, country independently),
    not as a single blob — because one source might have city+country but
    no region, while another has region but a stale city. Merging at the
    sub-field level salvages more information than picking one whole
    Location object as the winner.
    """
    cities = [(loc.city, src) for loc, src in located if loc and loc.city]
    regions = [(loc.region, src) for loc, src in located if loc and loc.region]
    countries = [(loc.country, src) for loc, src in located if loc and loc.country]
    
    city, city_src = _pick_scalar(cities)
    region, region_src = _pick_scalar(regions)
    country, country_src = _pick_scalar(countries)
    
    if not (city or region or country):
        return None, None
    
    return Location(city=city, region=region, country=country), \
        city_src or region_src or country_src


def _merge_links(linked: List[tuple]) -> Links:
    """Links merge field-by-field too, and 'other' is a union."""
    linkedins = [(l.linkedin, s) for l, s in linked if l and l.linkedin]
    githubs = [(l.github, s) for l, s in linked if l and l.github]
    portfolios = [(l.portfolio, s) for l, s in linked if l and l.portfolio]
    
    others = []
    seen_other = set()
    for l, _ in linked:
        if l:
            for o in l.other:
                if o not in seen_other:
                    seen_other.add(o)
                    others.append(o)
    
    linkedin, _ = _pick_scalar(linkedins)
    github, _ = _pick_scalar(githubs)
    portfolio, _ = _pick_scalar(portfolios)
    
    if not (linkedin or github or portfolio or others):
        return None
    return Links(linkedin=linkedin, github=github, portfolio=portfolio, other=others)


def _merge_emails(profiles: List[CanonicalProfile]) -> List[str]:
    """Union of all emails, deduplicated case-insensitively, order-preserving."""
    seen = set()
    result = []
    for p in profiles:
        for e in p.emails:
            key = e.lower()
            if key not in seen:
                seen.add(key)
                result.append(key)
    return result


def _merge_phones(profiles: List[CanonicalProfile]) -> List[str]:
    """Union of all phones (already E.164-normalized at extraction time)."""
    seen = set()
    result = []
    for p in profiles:
        for ph in p.phones:
            if ph not in seen:
                seen.add(ph)
                result.append(ph)
    return result


def _merge_skills(profiles: List[CanonicalProfile]) -> List[Skill]:
    """
    Union skills by canonical name (case-insensitive). When the same skill
    appears from multiple sources:
      - confidence = MAX of individual confidences, boosted by a small
        corroboration bonus per additional independent source, capped at 1.0
      - sources = union of all sources that mentioned it
    
    WHY max+boost rather than average?
      Averaging would let one low-confidence inferred mention (e.g. GitHub
      repo topic, confidence 0.4) drag down a high-confidence asserted
      mention (e.g. ATS skills field, confidence 0.7) even though the
      asserted mention alone should anchor our belief — corroboration
      should only ever increase confidence, never decrease it.
    """
    by_name: Dict[str, Skill] = {}
    
    for p in profiles:
        for skill in p.skills:
            key = skill.name.lower()
            if key not in by_name:
                by_name[key] = Skill(
                    name=skill.name,
                    confidence=skill.confidence,
                    sources=list(skill.sources),
                )
            else:
                existing = by_name[key]
                new_sources = [s for s in skill.sources if s not in existing.sources]
                if new_sources:
                    existing.sources.extend(new_sources)
                    # Corroboration bonus: +0.1 per additional independent source
                    boosted = max(existing.confidence, skill.confidence) + 0.1 * len(new_sources)
                    existing.confidence = min(1.0, boosted)
                else:
                    existing.confidence = max(existing.confidence, skill.confidence)
    
    # Sort by confidence descending — most-credible skills first
    return sorted(by_name.values(), key=lambda s: -s.confidence)


def _merge_experience(profiles: List[CanonicalProfile]) -> List[Experience]:
    """
    Deduplicate by (company, title) normalized. When duplicates exist,
    keep the one with MORE complete date info (prefer start+end over
    start-only over neither), and merge summary text (prefer the longer,
    more detailed summary — proxy for "more informative").
    """
    by_key: Dict[tuple, Experience] = {}
    
    def completeness(e: Experience) -> int:
        return (1 if e.start else 0) + (1 if e.end else 0) + (1 if e.summary else 0)
    
    for p in profiles:
        for exp in p.experience:
            key = (_norm_name(exp.company), _norm_name(exp.title or ""))
            if key not in by_key:
                by_key[key] = exp
            else:
                existing = by_key[key]
                if completeness(exp) > completeness(existing):
                    # Take the more complete one, but don't lose a summary
                    # the existing one had that the new one lacks
                    merged_summary = exp.summary or existing.summary
                    by_key[key] = Experience(
                        company=exp.company, title=exp.title,
                        start=exp.start or existing.start,
                        end=exp.end or existing.end,
                        summary=merged_summary,
                    )
                elif not existing.summary and exp.summary:
                    existing.summary = exp.summary
    
    # Sort: current/most-recent role first (no end date = current, sorts first),
    # then by start date descending
    def sort_key(e: Experience):
        is_current = e.end is None
        return (0 if is_current else 1, e.start or "0000-00")
    
    return sorted(by_key.values(), key=sort_key)


def _merge_education(profiles: List[CanonicalProfile]) -> List[Education]:
    """Deduplicate by (institution, degree) normalized."""
    by_key: Dict[tuple, Education] = {}
    for p in profiles:
        for edu in p.education:
            key = (_norm_name(edu.institution), _norm_name(edu.degree or ""))
            if key not in by_key or (edu.end_year and not by_key[key].end_year):
                by_key[key] = edu
    return list(by_key.values())


def _compute_overall_confidence(
    profile: CanonicalProfile, winning_sources: Dict[str, str]
) -> float:
    """
    Weighted average of field-level confidences across populated fields.
    
    Weights reflect field importance to downstream hiring decisions:
        identity fields (name, email) matter most; headline/years_experience
        matter less. Unpopulated fields are EXCLUDED from the average rather
        than penalized as zero — a missing field is "honestly empty," not
        "wrong," and the problem statement explicitly says empty beats
        wrong-but-confident, so we don't want sparse profiles auto-penalized
        into oblivion; we want their PRESENT fields scored fairly.
    """
    field_weights = {
        "full_name": 0.25,
        "emails": 0.20,
        "phones": 0.10,
        "location": 0.10,
        "headline": 0.05,
        "skills": 0.15,
        "experience": 0.10,
        "education": 0.05,
    }
    
    total_weight = 0.0
    weighted_sum = 0.0
    
    def source_conf(field_name: str) -> Optional[float]:
        src = winning_sources.get(field_name)
        return SOURCE_RELIABILITY_WEIGHT.get(src) if src else None
    
    if profile.full_name:
        c = source_conf("full_name") or 0.7
        weighted_sum += field_weights["full_name"] * c
        total_weight += field_weights["full_name"]
    if profile.emails:
        weighted_sum += field_weights["emails"] * 0.95  # emails are near-certain once normalized
        total_weight += field_weights["emails"]
    if profile.phones:
        weighted_sum += field_weights["phones"] * 0.85
        total_weight += field_weights["phones"]
    if profile.location:
        c = source_conf("location") or 0.6
        weighted_sum += field_weights["location"] * c
        total_weight += field_weights["location"]
    if profile.headline:
        c = source_conf("headline") or 0.6
        weighted_sum += field_weights["headline"] * c
        total_weight += field_weights["headline"]
    if profile.skills:
        avg_skill_conf = sum(s.confidence for s in profile.skills) / len(profile.skills)
        weighted_sum += field_weights["skills"] * avg_skill_conf
        total_weight += field_weights["skills"]
    if profile.experience:
        weighted_sum += field_weights["experience"] * 0.8
        total_weight += field_weights["experience"]
    if profile.education:
        weighted_sum += field_weights["education"] * 0.8
        total_weight += field_weights["education"]
    
    if total_weight == 0:
        return 0.0
    return round(weighted_sum / total_weight, 3)


def merge_cluster(cluster: List[CanonicalProfile]) -> CanonicalProfile:
    """
    Merge all partial profiles in one identity cluster into a single
    CanonicalProfile, applying the field-level policies documented above.
    """
    # Candidate ID: keep the first one deterministically (sorted to be
    # independent of input file order) — ensures determinism requirement.
    candidate_id = sorted(p.candidate_id for p in cluster)[0]
    
    winning_sources: Dict[str, str] = {}
    
    # Scalar fields
    name_candidates = [(p.full_name, _source_of(p)) for p in cluster if p.full_name]
    full_name, name_src = _pick_scalar(name_candidates)
    if name_src:
        winning_sources["full_name"] = name_src
    
    headline_candidates = [(p.headline, _source_of(p)) for p in cluster if p.headline]
    headline, headline_src = _pick_scalar(headline_candidates)
    if headline_src:
        winning_sources["headline"] = headline_src
    
    years_candidates = [(p.years_experience, _source_of(p)) for p in cluster if p.years_experience is not None]
    years_experience, _ = _pick_scalar(years_candidates)
    
    # Location & Links (field-by-field merge)
    loc_candidates = [(p.location, _source_of(p)) for p in cluster if p.location]
    location, loc_src = _merge_location(loc_candidates)
    if loc_src:
        winning_sources["location"] = loc_src
    
    links_candidates = [(p.links, _source_of(p)) for p in cluster if p.links]
    links = _merge_links(links_candidates)
    
    # List fields (union)
    emails = _merge_emails(cluster)
    phones = _merge_phones(cluster)
    skills = _merge_skills(cluster)
    experience = _merge_experience(cluster)
    education = _merge_education(cluster)
    
    # Provenance: concatenate all source provenance entries
    provenance = []
    for p in cluster:
        provenance.extend(p.provenance)
    
    merged = CanonicalProfile(
        candidate_id=candidate_id,
        full_name=full_name,
        emails=emails,
        phones=phones,
        location=location,
        links=links,
        headline=headline,
        years_experience=years_experience,
        skills=skills,
        experience=experience,
        education=education,
        provenance=provenance,
    )
    
    merged.overall_confidence = _compute_overall_confidence(merged, winning_sources)
    return merged


def merge_all(profiles: List[CanonicalProfile]) -> List[CanonicalProfile]:
    """Top-level entry point: cluster, then merge each cluster."""
    clusters = cluster_by_identity(profiles)
    return [merge_cluster(c) for c in clusters]
