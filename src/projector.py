"""
projector.py — project a CanonicalProfile into a
custom output shape defined by a runtime config, WITHOUT changing the
engine or writing new code.


KEY DESIGN DECISIONS:

1. "path" vs "from" — why two keys?
    "path" is ALWAYS the name of the field in the OUTPUT JSON (what the
    caller wants it called). "from" is OPTIONAL and specifies where to read
    the value from in the canonical record, using a small JSON-path-like
    mini-language (see _resolve_from below). If "from" is omitted, we
    default to reading the canonical field with the SAME name as "path".

2. Why a mini JSON-path language instead of full JSONPath/jq?
    We only need a handful of access patterns:
        - "field"            simple field
        - "field[0]"         first element of a list
        - "field[].subfield" map: pull subfield out of every list item
    A ~30-line resolver handles every case the example config and our
    schema require, and is easy to read/audit — important since
    "explainable" is a constraint.

3. on_missing semantics:
        "null"  -> output field is set to null (default; matches example config)
        "omit"  -> output field is not included in the dict at all
        "error" -> raise ProjectionError listing every missing required field
                (collected, not fail-fast, so the caller gets the full
                picture in one pass)

4. normalize per-field at projection time is about RESHAPING for output
    (e.g. join list to comma string), distinct from ingestion-time
    canonicalization which already happened during merge.

5. required + validation: the projector builds the dict; validator.py
    validates it. Separate steps, single-responsibility.
"""

import re
import logging
from typing import Any, Dict, List, Optional

from .schema import CanonicalProfile

logger = logging.getLogger(__name__)


class ProjectionError(Exception):
    """Raised when on_missing == 'error' and required fields are absent."""
    pass


# Mini JSON-path resolver

_PATH_TOKEN_RE = re.compile(r'^([a-zA-Z_][a-zA-Z0-9_]*)(\[(\d*)\])?$')


def _get_attr_or_key(obj: Any, name: str) -> Any:
    """Read `name` off obj whether obj is a dict or a dataclass/object."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _resolve_from(profile_dict: Dict[str, Any], from_path: str) -> Any:
    """
    Resolve a 'from' path against the canonical profile's dict form.
    Supported syntax:
        "full_name"                -> profile_dict["full_name"]
        "emails[0]"                -> profile_dict["emails"][0]
        "skills[].name"            -> [s["name"] for s in profile_dict["skills"]]
        "experience[0].company"    -> profile_dict["experience"][0]["company"]

    Returns None if any segment is missing/out of range — NEVER raises,
    because "missing" is an expected case (on_missing policy handles it).
    """
    segments = from_path.split(".")
    current: Any = profile_dict

    for i, seg in enumerate(segments):
        if current is None:
            return None

        m = _PATH_TOKEN_RE.match(seg)
        if not m:
            logger.warning(f"Unparseable path segment: {seg!r} in {from_path!r}")
            return None

        field_name, has_index, index_str = m.group(1), m.group(2), m.group(3)

        current = _get_attr_or_key(current, field_name)
        if current is None:
            return None

        if has_index:
            if not isinstance(current, list):
                return None
            if index_str == "":
                # "field[]" -> map remaining path over every list item
                remaining = ".".join(segments[i + 1:])
                if not remaining:
                    return current  # "skills[]" alone -> the whole list
                mapped = []
                for item in current:
                    if isinstance(item, dict):
                        mapped.append(_resolve_from(item, remaining))
                    else:
                        mapped.append(item)
                return mapped
            else:
                idx = int(index_str)
                current = current[idx] if 0 <= idx < len(current) else None

    return current


# Output-time normalization (formatting transforms)

def _apply_output_normalize(value: Any, normalize: Optional[str]) -> Any:
    """
    Apply a named output-formatting transform. Unknown normalize names are
    a no-op (with a warning) rather than an error — forward-compatible.
    """
    if normalize is None or value is None:
        return value

    if normalize == "E164":
        # Already E.164 from ingestion; pass-through.
        return value

    if normalize == "canonical":
        # Already canonicalized at ingestion (skill names). Pass through.
        return value

    if normalize == "join_comma":
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return value

    if normalize == "uppercase":
        return str(value).upper() if value is not None else value

    if normalize == "lowercase":
        return str(value).lower() if value is not None else value

    logger.warning(f"Unknown normalize directive: {normalize!r} — passing through")
    return value


# Type coercion / checking

def _coerce_type(value: Any, declared_type: Optional[str], field_path: str) -> Any:
    """
    Best-effort coercion to the declared type. We coerce rather than reject
    where safe, but never invent data — None stays None regardless of type.
    """
    if value is None or declared_type is None:
        return value

    try:
        if declared_type == "string":
            return value if isinstance(value, str) else str(value)
        if declared_type == "number":
            return value if isinstance(value, (int, float)) else float(value)
        if declared_type == "string[]":
            if isinstance(value, list):
                return [v if isinstance(v, str) else str(v) for v in value]
            return [str(value)]
        if declared_type == "boolean":
            return bool(value)
    except (ValueError, TypeError):
        logger.warning(f"Type coercion failed for field {field_path!r}: "
                        f"value={value!r} declared_type={declared_type!r}")
        return value

    return value


# Main projection entry point

DEFAULT_CONFIG = {
    "fields": [
        {"path": "candidate_id", "type": "string", "required": True},
        {"path": "full_name", "type": "string", "required": True},
        {"path": "emails", "type": "string[]"},
        {"path": "phones", "type": "string[]", "normalize": "E164"},
        {"path": "location", "type": "object"},
        {"path": "links", "type": "object"},
        {"path": "headline", "type": "string"},
        {"path": "years_experience", "type": "number"},
        {"path": "skills", "type": "object[]"},
        {"path": "experience", "type": "object[]"},
        {"path": "education", "type": "object[]"},
        {"path": "overall_confidence", "type": "number"},
    ],
    "include_confidence": True,
    "on_missing": "null",
}
# WHY this exact default?
#   It mirrors the "Default output schema" table from the problem statement
#   one-to-one. Running with no config at all reproduces that table.


def project(profile: CanonicalProfile, config: Optional[Dict] = None) -> Dict[str, Any]:
    """
    Build the output dict for one profile according to config.
    Raises ProjectionError if on_missing == "error" and any required
    field ends up unresolved.
    """
    cfg = config if config is not None else DEFAULT_CONFIG
    fields_cfg = cfg.get("fields", DEFAULT_CONFIG["fields"])
    include_confidence = cfg.get("include_confidence", True)
    on_missing = cfg.get("on_missing", "null")

    if on_missing not in ("null", "omit", "error"):
        logger.warning(f"Unknown on_missing policy {on_missing!r}; defaulting to 'null'")
        on_missing = "null"

    profile_dict = profile.to_dict()
    output: Dict[str, Any] = {}
    missing_required: List[str] = []

    for field_cfg in fields_cfg:
        out_path = field_cfg["path"]
        from_path = field_cfg.get("from", out_path)
        declared_type = field_cfg.get("type")
        required = field_cfg.get("required", False)
        normalize = field_cfg.get("normalize")

        value = _resolve_from(profile_dict, from_path)
        value = _apply_output_normalize(value, normalize)
        value = _coerce_type(value, declared_type, out_path)

        is_missing = value is None or value == [] or value == ""

        if is_missing:
            if required:
                missing_required.append(out_path)
            if on_missing == "omit":
                continue  # don't add the key at all
            output[out_path] = None  # "null" (and "error", pending the check below)
        else:
            output[out_path] = value

    # Confidence / provenance toggle — applied AFTER per-field projection
    # because it's a profile-level concern, not a single field's concern.
    if include_confidence:
        output["overall_confidence"] = profile.overall_confidence
        output["provenance"] = [p.to_dict() for p in profile.provenance]
    else:
        output.pop("overall_confidence", None)
        output.pop("provenance", None)

    if on_missing == "error" and missing_required:
        raise ProjectionError(
            f"Candidate {profile.candidate_id}: missing required field(s): "
            f"{', '.join(missing_required)}"
        )

    return output


def project_all(profiles: List[CanonicalProfile], config: Optional[Dict] = None) -> List[Dict[str, Any]]:
    """
    Project a list of profiles. A single candidate's projection error
    (on_missing='error') does NOT abort the whole batch — robustness
    requirement: one bad/garbage record must not crash the run. We log
    and skip that candidate.
    """
    results = []
    for profile in profiles:
        try:
            results.append(project(profile, config))
        except ProjectionError as e:
            logger.error(f"Projection failed, skipping candidate: {e}")
    return results
