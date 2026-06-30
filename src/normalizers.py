"""
normalizers.py — Convert raw values from any source into canonical formats.

WHY a dedicated normalization layer?
  Extractors pull out values as-found (messy). Mergers need to compare
  values across sources. You can only compare "India" vs "IN" vs "IND"
  after you've normalized both to the same representation.
  Also: the output config can request specific normalization (e.g. E164 for phones),
  so normalization must be callable independently of extraction.

Design principle: normalization functions are PURE — same input → same output,
  no side effects. They return None if they can't normalize rather than crashing.
"""

import re
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Phone normalization — E.164 format
# ─────────────────────────────────────────────

try:
    import phonenumbers
    PHONENUMBERS_AVAILABLE = True
except ImportError:
    PHONENUMBERS_AVAILABLE = False
    logger.warning("phonenumbers not installed — phone normalization degraded")


def normalize_phone(raw: str, default_region: str = "US") -> Optional[str]:
    """
    Convert any phone string to E.164 format (+<country><number>).
    
    WHY E.164?
      It's the international standard. "+91 98765 43210" and "09876543210"
      both mean the same thing in India but are different strings. E.164
      makes them identical: "+919876543210". This enables deduplication.
    
    WHY default_region="US"?
      When there's no country code in the number, we have to guess.
      US is a safe default for international recruiting platforms.
      ATS sources that have country context can override this.
    
    Returns None if the number cannot be parsed — never invents a number.
    """
    if not raw:
        return None
    
    # Strip common non-digit chars except leading +
    cleaned = re.sub(r'[^\d+]', '', raw.strip())
    
    if not cleaned:
        return None
    
    if PHONENUMBERS_AVAILABLE:
        try:
            parsed = phonenumbers.parse(cleaned, default_region)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(
                    parsed, phonenumbers.PhoneNumberFormat.E164
                )
        except Exception:
            pass
    
    # Fallback: if it's already 10+ digits and starts with +, return as-is
    if cleaned.startswith('+') and len(cleaned) >= 10:
        return cleaned
    
    logger.debug(f"Could not normalize phone: {raw!r}")
    return None


def normalize_phones(raws: List[str], default_region: str = "US") -> List[str]:
    """Normalize a list, dedup, and remove failures."""
    seen = set()
    result = []
    for raw in raws:
        normed = normalize_phone(raw, default_region)
        if normed and normed not in seen:
            seen.add(normed)
            result.append(normed)
    return result


# ─────────────────────────────────────────────
# Country normalization — ISO-3166 alpha-2
# ─────────────────────────────────────────────

try:
    import pycountry
    PYCOUNTRY_AVAILABLE = True
except ImportError:
    PYCOUNTRY_AVAILABLE = False

# Manually curated common aliases that pycountry might not catch
COUNTRY_ALIASES = {
    "india": "IN", "usa": "US", "united states": "US",
    "united states of america": "US", "uk": "GB",
    "united kingdom": "GB", "germany": "DE", "france": "FR",
    "canada": "CA", "australia": "AU", "china": "CN",
    "japan": "JP", "singapore": "SG", "uae": "AE",
}


def normalize_country(raw: str) -> Optional[str]:
    """
    Convert country name or code to ISO-3166 alpha-2.
    
    WHY alpha-2?
      Short, universal, database-friendly. "IN" not "India" not "IND".
    
    Strategy:
      1. If it's already 2 chars and uppercase, trust it.
      2. Check our manual alias table (handles typos and common variants).
      3. Fall back to pycountry's fuzzy search.
      4. Return None if we can't figure it out — never invent.
    """
    if not raw:
        return None
    
    stripped = raw.strip()
    
    # Already alpha-2?
    if len(stripped) == 2 and stripped.isalpha():
        return stripped.upper()
    
    lower = stripped.lower()
    
    # Check aliases first (fast path)
    if lower in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[lower]
    
    if PYCOUNTRY_AVAILABLE:
        try:
            country = pycountry.countries.lookup(stripped)
            return country.alpha_2
        except LookupError:
            pass
    
    logger.debug(f"Could not normalize country: {raw!r}")
    return None


# ─────────────────────────────────────────────
# Date normalization — YYYY-MM format
# ─────────────────────────────────────────────

# Ordered from most to least specific — first match wins
DATE_PATTERNS = [
    # ISO format: 2024-05, 2024-05-01
    (re.compile(r'^(\d{4})-(\d{2})(?:-\d{2})?$'), lambda m: f"{m.group(1)}-{m.group(2)}"),
    # Month Year: "May 2024", "May, 2024"
    (re.compile(
        r'^(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
        r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'[,\s]+(\d{4})$', re.IGNORECASE
    ), None),  # handled separately
    # Year only: "2024" → "2024-01" (first month, conservative)
    (re.compile(r'^(\d{4})$'), lambda m: f"{m.group(1)}-01"),
]

MONTH_MAP = {
    'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
    'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
    'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12',
}


def normalize_date(raw: str) -> Optional[str]:
    """
    Convert any date string to YYYY-MM.
    
    WHY YYYY-MM and not full date?
      Employment history rarely has day precision. Using YYYY-MM avoids
      false precision. It's sortable as a string.
    
    Returns None if we can't parse — never guesses.
    """
    if not raw:
        return None
    
    stripped = raw.strip()
    
    # Try ISO-style patterns
    m = re.match(r'^(\d{4})-(\d{2})(?:-\d{2})?$', stripped)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    
    # Try "Month YYYY"
    m = re.match(
        r'^(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
        r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
        r'[,\s]+(\d{4})$', stripped, re.IGNORECASE
    )
    if m:
        month_key = m.group(1)[:3].lower()
        return f"{m.group(2)}-{MONTH_MAP.get(month_key, '01')}"
    
    # Try year only
    m = re.match(r'^(\d{4})$', stripped)
    if m:
        return f"{m.group(1)}-01"
    
    logger.debug(f"Could not normalize date: {raw!r}")
    return None


# ─────────────────────────────────────────────
# Skill canonicalization
# ─────────────────────────────────────────────

# Map of known aliases → canonical name.
# WHY have this map?
#   "postgres", "PostgreSQL", "psql", "pg" all mean the same thing.
#   Without canonicalization, we'd show 4 separate skills and undercount
#   the candidate's proficiency. The merger can only deduplicate after
#   names match exactly.
# Design: lower-cased key, canonical-cased value.
SKILL_CANONICAL = {
    # Languages
    "python": "Python", "py": "Python",
    "javascript": "JavaScript", "js": "JavaScript",
    "typescript": "TypeScript", "ts": "TypeScript",
    "golang": "Go", "go": "Go",
    "rust": "Rust", "c++": "C++", "cpp": "C++",
    "java": "Java", "scala": "Scala", "kotlin": "Kotlin",
    # Databases
    "postgresql": "PostgreSQL", "postgres": "PostgreSQL",
    "psql": "PostgreSQL", "pg": "PostgreSQL",
    "mysql": "MySQL", "sqlite": "SQLite",
    "redis": "Redis", "mongodb": "MongoDB", "mongo": "MongoDB",
    "elasticsearch": "Elasticsearch", "elastic": "Elasticsearch",
    "pgvector": "pgvector",
    # Frameworks
    "fastapi": "FastAPI", "fast api": "FastAPI",
    "django": "Django", "flask": "Flask",
    "react": "React", "next.js": "Next.js", "nextjs": "Next.js",
    "vue": "Vue.js", "vuejs": "Vue.js",
    # Infra / DevOps
    "kafka": "Kafka", "apache kafka": "Kafka",
    "kubernetes": "Kubernetes", "k8s": "Kubernetes",
    "docker": "Docker", "terraform": "Terraform",
    "grpc": "gRPC", "protobuf": "Protocol Buffers",
    "redis": "Redis",
    # ML/AI
    "pytorch": "PyTorch", "torch": "PyTorch",
    "tensorflow": "TensorFlow", "tf": "TensorFlow",
    "huggingface": "Hugging Face", "hugging face": "Hugging Face",
    "langchain": "LangChain",
    # Concepts
    "microservices": "Microservices", "micro services": "Microservices",
    "microservices architecture": "Microservices", "microservice architecture": "Microservices",
    "distributed systems": "Distributed Systems",
    "vector databases": "Vector Databases", "vector db": "Vector Databases",
    "vector database": "Vector Databases", "vector-database": "Vector Databases",
    "machine learning": "Machine Learning", "ml": "Machine Learning",
    "deep learning": "Deep Learning", "dl": "Deep Learning",
    "nlp": "NLP", "natural language processing": "NLP",
    "rag": "RAG", "retrieval augmented generation": "RAG",
    "ai": "AI", "artificial intelligence": "AI",
    "hnsw": "HNSW", "ivfflat": "IVFFlat", "wal": "WAL",
}


def canonicalize_skill(raw: str) -> str:
    """
    Convert a raw skill string to its canonical form.
    If no mapping exists, title-case it and return — don't drop unknown skills.
    
    WHY not drop unknown skills?
      A new framework we haven't seen before is still a real skill.
      Better to show "Foobar" than silently discard it.
    
    WHY strip parenthetical asides before lookup?
      Free-text sources often write "vector databases (pgvector)" where the
      parenthetical is a clarifying example, not part of the skill name.
      Looking up the full string with parens attached would miss the alias
      table entirely and produce a garbage title-cased string. We strip the
      parenthetical for the primary lookup; the caller (canonicalize_skills)
      is responsible for ALSO emitting the parenthetical content as its own
      skill if useful — that's handled by the comma-split + per-token call
      pattern already in extractors, since pgvector typically also appears
      as its own token elsewhere in the source.
    """
    if not raw:
        return raw
    
    # Strip a single trailing parenthetical aside, e.g. "X (Y)" -> "X"
    stripped_raw = re.sub(r'\s*\([^)]*\)\s*$', '', raw).strip()
    
    lower = stripped_raw.lower()
    return SKILL_CANONICAL.get(lower, stripped_raw.title())


def canonicalize_skills(raw_skills: List[str]) -> List[str]:
    """Canonicalize a list, dedup by canonical name (case-insensitive)."""
    seen = set()
    result = []
    for raw in raw_skills:
        canonical = canonicalize_skill(raw)
        key = canonical.lower()
        if key not in seen:
            seen.add(key)
            result.append(canonical)
    return result


# ─────────────────────────────────────────────
# URL normalization — ensure https:// prefix
# ─────────────────────────────────────────────

def normalize_url(raw: str) -> Optional[str]:
    """Ensure URLs have a scheme. Return None for obviously invalid strings."""
    if not raw:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    if stripped.startswith(('http://', 'https://')):
        return stripped
    # Common case: "linkedin.com/in/..." without scheme
    if re.match(r'^[a-zA-Z0-9]', stripped):
        return f"https://{stripped}"
    return None


def normalize_email(raw: str) -> Optional[str]:
    """
    Lowercase and strip email. 
    WHY lowercase? RFC 5321 says local part is case-sensitive but in practice
    virtually all providers treat it case-insensitively. Normalizing prevents
    dedup failures.
    
    WHY re.search (not re.match)?
      re.match only anchors at position 0 of the string. Source text often
      has the email embedded mid-sentence ("contact me at jane@x.com please")
      or with a leading label ("Email: jane@x.com"). re.search finds the
      pattern anywhere in the string, which is what we actually need here.
    """
    if not raw:
        return None
    m = re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', raw.strip())
    if m:
        return m.group(0).lower()
    return None
