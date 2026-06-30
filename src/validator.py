"""
validator.py — Validate the PROJECTED output (not the internal canonical
record) against the requested schema before returning it to the caller.

WHY validate the projected output and not the canonical record?
    The canonical record is our internal contract — we control it, it's
    always shaped correctly because schema.py guarantees it. The PROJECTED
    output is what an external caller's config asked for — it can go wrong
    in ways the canonical record can't: a bad "from" path, a type mismatch
    after a normalize transform, a required field that simply has no data
    anywhere in any source. That surface is the actual risk; that's what
    needs validation.

WHY a separate module rather than inlining checks in projector.py?
    Single-responsibility again: the projector's job is to BUILD a shape;
    the validator's job is to JUDGE a shape against rules. Keeping them
    separate means we can validate ANY dict (not just ones the projector
    built) — useful for tests, and for validating gold-standard comparisons.

DESIGN: validation never raises for "graceful degrade" cases. It returns
a structured ValidationResult (errors + warnings) so callers can decide
what to do — log and continue, drop the record, or hard-fail, matching
the "validate output before returning it; degrade gracefully on
missing/garbage" requirement.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_TYPE_CHECKERS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "string[]": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
    "object": lambda v: isinstance(v, dict),
    "object[]": lambda v: isinstance(v, list) and all(isinstance(x, dict) for x in v),
}


@dataclass
class ValidationResult:
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"is_valid": self.is_valid, "errors": self.errors, "warnings": self.warnings}


def validate_output(output: Dict[str, Any], config: Optional[Dict] = None) -> ValidationResult:
    """
    Validate one projected output dict against the field config it was
    built from.

    Checks performed (each is independent — we collect ALL problems in one
    pass rather than stopping at the first, matching the "explainable"
    and "no silent failure" goals):

        1. Required fields present and non-null (on_missing='null' can still
            leave a None in place for a required field — that's a genuine
            validation FAILURE the caller needs to know about, distinct from
            on_missing='error' which raises during projection itself; this
            catches the same problem when on_missing='null' was chosen and
            the caller still wants visibility).
        2. Declared type matches actual Python type of the value.
        3. No empty-string values for required string fields (an empty
            string technically "is a string" but is semantically missing).

    A missing/garbage canonical profile must not crash this function —
    we wrap each check and downgrade unexpected exceptions to a warning
    rather than letting validation itself blow up the pipeline.
    """
    from .projector import DEFAULT_CONFIG
    cfg = config if config is not None else DEFAULT_CONFIG
    fields_cfg = cfg.get("fields", DEFAULT_CONFIG["fields"])

    errors: List[str] = []
    warnings: List[str] = []

    for field_cfg in fields_cfg:
        path = field_cfg["path"]
        declared_type = field_cfg.get("type")
        required = field_cfg.get("required", False)

        try:
            present = path in output
            value = output.get(path)

            is_empty = value is None or value == [] or value == ""

            if required and (not present or is_empty):
                errors.append(f"Required field '{path}' is missing or empty")
                continue  # can't usefully type-check a missing value

            if not present:
                # Field was omitted (on_missing='omit') and wasn't required
                # — that's expected behavior, not a problem.
                continue

            if value is not None and declared_type and declared_type in _TYPE_CHECKERS:
                checker = _TYPE_CHECKERS[declared_type]
                if not checker(value):
                    errors.append(
                        f"Field '{path}' has type {type(value).__name__}, "
                        f"expected {declared_type}"
                    )
        except Exception as e:
            # Validation itself must never crash the pipeline.
            warnings.append(f"Validation check for '{path}' raised unexpectedly: {e}")

    is_valid = len(errors) == 0
    return ValidationResult(is_valid=is_valid, errors=errors, warnings=warnings)


def validate_all(outputs: List[Dict[str, Any]], config: Optional[Dict] = None) -> List[ValidationResult]:
    """Validate a batch. Returns one ValidationResult per output, same order."""
    return [validate_output(o, config) for o in outputs]
