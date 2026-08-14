"""Guardian PII redaction and local re-hydration (I3, I4).

Pure functions only: no database, no model call, no network.
Never log the input text or the redaction map.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from packages.atlas.models import Passenger

# kinds_found vocabulary — stable strings for the Guardian UI pane.
_KIND_NAME = "passenger_name"
_KIND_PASSPORT = "passport"
_KIND_DOB = "dob"
_KIND_PAN = "pan"

_PAN_RE = re.compile(r"(?<!\d)(\d{13,19})(?!\d)")
# ICAO-ish travel document ids: 1–2 letters + 6–9 digits (e.g. A12345678).
_PASSPORT_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,2}\d{6,9})(?![A-Za-z0-9])")
# Numeric DOB shapes commonly seen in free text / forms.
_DOB_SHAPE_RE = re.compile(
    r"(?<!\d)("
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{4}/\d{2}/\d{2}"
    r"|\d{2}/\d{2}/\d{4}"
    r"|\d{2}-\d{2}-\d{4}"
    r"|\d{8}"
    r")(?!\d)"
)
_TOKEN_RE = re.compile(r"\[\[PAX_\d+_(?:NAME|GIVEN|SURNAME|PASSPORT|DOB|PAN)\]\]")


class RedactionMap(BaseModel):
    """Token -> real value. Zone A only. Never serialised outside the host (I4)."""

    tokens: dict[str, str]


class RedactionResult(BaseModel):
    text: str
    map: RedactionMap
    kinds_found: list[str]  # "passenger_name" | "passport" | "dob" | "pan"


class PIIDetectedError(ValueError):
    """Raised by assert_no_pii. Message names the kind only — never the value."""


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(digits[::-1]):
        n = ord(ch) - 48
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _as_date(value: datetime | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _dob_format_strings(d: date) -> list[str]:
    """Deterministic list of surface forms for a known DOB."""
    y, m, day = d.year, d.month, d.day
    return [
        f"{y:04d}-{m:02d}-{day:02d}",
        f"{y:04d}/{m:02d}/{day:02d}",
        f"{day:02d}/{m:02d}/{y:04d}",
        f"{m:02d}/{day:02d}/{y:04d}",
        f"{day:02d}-{m:02d}-{y:04d}",
        f"{m:02d}-{day:02d}-{y:04d}",
        f"{y:04d}{m:02d}{day:02d}",
        d.strftime("%d %b %Y"),
        d.strftime("%d %B %Y"),
        d.strftime("%b %d, %Y"),
        d.strftime("%B %d, %Y"),
    ]


def _find_ci(text: str, needle: str) -> list[tuple[int, int, str]]:
    """Case-insensitive occurrences; returned slice is the exact original text."""
    if not needle:
        return []
    pattern = re.compile(re.escape(needle), re.IGNORECASE)
    return [(m.start(), m.end(), m.group(0)) for m in pattern.finditer(text)]


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return not (a[1] <= b[0] or b[1] <= a[0])


def _plausible_dob(raw: str) -> bool:
    """Reject digit runs / dates that cannot be a calendar DOB."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) != 8:
        return False
    # Prefer YYYYmmdd when the first four digits look like a year.
    candidates: list[tuple[int, int, int]] = []
    y_first = (int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
    candidates.append(y_first)
    d_first = (int(digits[4:8]), int(digits[2:4]), int(digits[0:2]))
    candidates.append(d_first)
    m_first = (int(digits[4:8]), int(digits[0:2]), int(digits[2:4]))
    candidates.append(m_first)
    for y, m, d in candidates:
        if y < 1900 or y > 2100:
            continue
        try:
            date(y, m, d)
            return True
        except ValueError:
            continue
    return False


def redact(text: str, *, passengers: list[Passenger] | None = None) -> RedactionResult:
    """Replace passenger names, passport numbers, dates of birth and anything
    PAN-shaped with stable tokens like [[PAX_1_NAME]]. Deterministic: the same
    input yields the same tokens. Called on EVERY payload before Zone C egress.
    """
    # Span: (start, end, exact_value, token, kind)
    spans: list[tuple[int, int, str, str, str]] = []
    # Stable token assignment: first-seen exact value wins.
    value_to_token: dict[tuple[str, str], str] = {}
    next_index = {"n": 1}

    def token_for(kind_key: str, value: str, index_hint: int | None = None) -> str:
        map_key = (kind_key, value)
        existing = value_to_token.get(map_key)
        if existing is not None:
            return existing
        if index_hint is not None:
            idx = index_hint
            next_index["n"] = max(next_index["n"], idx + 1)
        else:
            idx = next_index["n"]
            next_index["n"] = idx + 1
        suffix = {
            "name": "NAME",
            "given": "GIVEN",
            "surname": "SURNAME",
            "passport": "PASSPORT",
            "dob": "DOB",
            "pan": "PAN",
        }[kind_key]
        tok = f"[[PAX_{idx}_{suffix}]]"
        value_to_token[map_key] = tok
        return tok

    def add_span(start: int, end: int, exact: str, kind_key: str, kind: str, index_hint: int | None = None) -> None:
        if start >= end or not exact:
            return
        # Skip if this region already covered by a longer/equal earlier span.
        region = (start, end)
        for s, e, _v, _t, _k in spans:
            if _overlaps(region, (s, e)):
                # Prefer the longer span; drop the shorter new one.
                if (e - s) >= (end - start):
                    return
        # Drop any existing spans fully overlapped by this longer one.
        kept: list[tuple[int, int, str, str, str]] = []
        for s, e, v, t, k in spans:
            if _overlaps(region, (s, e)) and (end - start) > (e - s):
                continue
            if _overlaps(region, (s, e)):
                # Equal length conflict: keep the earlier (deterministic by insertion).
                return
            kept.append((s, e, v, t, k))
        spans.clear()
        spans.extend(kept)
        tok = token_for(kind_key, exact, index_hint=index_hint)
        spans.append((start, end, exact, tok, kind))

    pax_list = list(passengers) if passengers else []

    # --- Passenger-linked redactions (stable index = list order) ---
    for i, pax in enumerate(pax_list, start=1):
        given = (pax.given_name or "").strip()
        surname = (pax.surname or "").strip()
        if given and surname:
            for form in (
                f"{given} {surname}",
                f"{surname}, {given}",
                f"{surname},{given}",
            ):
                for start, end, exact in _find_ci(text, form):
                    add_span(start, end, exact, "name", _KIND_NAME, index_hint=i)
        if given:
            for start, end, exact in _find_ci(text, given):
                add_span(start, end, exact, "given", _KIND_NAME, index_hint=i)
        if surname:
            for start, end, exact in _find_ci(text, surname):
                add_span(start, end, exact, "surname", _KIND_NAME, index_hint=i)

        if pax.passport_number:
            for start, end, exact in _find_ci(text, pax.passport_number.strip()):
                add_span(start, end, exact, "passport", _KIND_PASSPORT, index_hint=i)

        dob = _as_date(pax.date_of_birth)
        # Longer / more specific formats first.
        for form in sorted(set(_dob_format_strings(dob)), key=len, reverse=True):
            for start, end, exact in _find_ci(text, form):
                add_span(start, end, exact, "dob", _KIND_DOB, index_hint=i)

    # --- Shape-based detection (always on, even with no passenger list) ---
    for match in _PAN_RE.finditer(text):
        digits = match.group(1)
        if _luhn_ok(digits):
            add_span(match.start(1), match.end(1), digits, "pan", _KIND_PAN)

    for match in _PASSPORT_RE.finditer(text):
        value = match.group(1)
        add_span(match.start(1), match.end(1), value, "passport", _KIND_PASSPORT)

    for match in _DOB_SHAPE_RE.finditer(text):
        value = match.group(1)
        if _plausible_dob(value):
            add_span(match.start(1), match.end(1), value, "dob", _KIND_DOB)

    # Leftmost-longest wins; apply right-to-left so indices stay valid.
    accepted: list[tuple[int, int, str, str, str]] = []
    for span in sorted(spans, key=lambda s: (s[0], -(s[1] - s[0]))):
        if any(_overlaps((span[0], span[1]), (a[0], a[1])) for a in accepted):
            continue
        accepted.append(span)

    out_chars = text
    token_map: dict[str, str] = {}
    for start, end, exact, tok, _kind in sorted(accepted, key=lambda s: s[0], reverse=True):
        out_chars = out_chars[:start] + tok + out_chars[end:]
        token_map[tok] = exact

    # kinds_found in left-to-right first-occurrence order.
    kinds_ltr: list[str] = []
    seen_k: set[str] = set()
    for _start, _end, _exact, _tok, kind in sorted(accepted, key=lambda s: s[0]):
        if kind not in seen_k:
            seen_k.add(kind)
            kinds_ltr.append(kind)

    return RedactionResult(
        text=out_chars,
        map=RedactionMap(tokens=token_map),
        kinds_found=kinds_ltr,
    )


def rehydrate(text: str, map: RedactionMap) -> str:
    """Zone A only. A model never sees the map."""
    if not map.tokens:
        return text
    # Longer tokens first so PAX_10_* cannot be partially confused with PAX_1_*.
    result = text
    for tok in sorted(map.tokens.keys(), key=len, reverse=True):
        result = result.replace(tok, map.tokens[tok])
    return result


def redact_image_metadata(image_bytes: bytes) -> bytes:
    """Strips EXIF, including GPS, before any image reaches Zone C.

    JPEG: drops APP1 (Exif / XMP) segments; pixel/entropy payload preserved.
    PNG: drops eXIf / tEXt / zTXt / iTXt ancillary chunks that can carry GPS.
    Other formats: returned unchanged (no metadata parser available in-stdlib).
    """
    if len(image_bytes) >= 2 and image_bytes[:2] == b"\xff\xd8":
        return _strip_jpeg_exif(image_bytes)
    if len(image_bytes) >= 8 and image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return _strip_png_metadata(image_bytes)
    return image_bytes


def _strip_jpeg_exif(data: bytes) -> bytes:
    out = bytearray()
    out.extend(b"\xff\xd8")
    i = 2
    n = len(data)
    while i < n:
        # Skip fill bytes.
        if data[i] != 0xFF:
            # Non-marker data before SOS should not happen; copy remainder.
            out.extend(data[i:])
            break
        # Consume 0xFF padding.
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            break
        marker = data[i]
        i += 1

        # Standalone markers (no length).
        if marker == 0xD9:  # EOI
            out.extend(b"\xff\xd9")
            break
        if marker == 0xD8:  # nested SOI — keep
            out.extend(b"\xff\xd8")
            continue
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out.extend(bytes((0xFF, marker)))
            continue

        if i + 2 > n:
            break
        seglen = int.from_bytes(data[i : i + 2], "big")
        if seglen < 2 or i + seglen > n:
            # Malformed — copy rest raw to avoid data loss.
            out.extend(data[i - 1 :])
            break
        segment = data[i : i + seglen]  # length bytes + payload
        i += seglen

        if marker == 0xDA:  # SOS — copy scan data through EOI/rest
            out.extend(bytes((0xFF, marker)))
            out.extend(segment)
            out.extend(data[i:])
            break

        # APP1 holds Exif (incl. GPS) and often XMP — strip entirely.
        if marker == 0xE1:
            continue

        out.extend(bytes((0xFF, marker)))
        out.extend(segment)

    return bytes(out)


def _strip_png_metadata(data: bytes) -> bytes:
    out = bytearray(data[:8])
    i = 8
    n = len(data)
    drop = {b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME"}
    while i + 8 <= n:
        length = int.from_bytes(data[i : i + 4], "big")
        ctype = data[i + 4 : i + 8]
        chunk_end = i + 12 + length
        if chunk_end > n:
            out.extend(data[i:])
            break
        if ctype not in drop:
            out.extend(data[i:chunk_end])
        i = chunk_end
        if ctype == b"IEND":
            break
    return bytes(out)


def _string_has_pii(value: str) -> str | None:
    """Return a kind name if PII remains, else None. Never returns the value."""
    # Already-tokenised placeholders are safe.
    scrubbed = _TOKEN_RE.sub("", value)

    for match in _PAN_RE.finditer(scrubbed):
        if _luhn_ok(match.group(1)):
            return _KIND_PAN
    if _PASSPORT_RE.search(scrubbed):
        return _KIND_PASSPORT
    for match in _DOB_SHAPE_RE.finditer(scrubbed):
        if _plausible_dob(match.group(1)):
            return _KIND_DOB
    return None


def assert_no_pii(payload: dict) -> None:
    """Raises if any PAN-shaped, passport-shaped or known-passenger-name value
    is present. Called immediately before Zone B and Zone C egress.

    Also rejects DOB-shaped residual strings. Messages name the kind only —
    never the offending value (I4).
    """
    # Known passenger-name field keys: a non-empty value under these is PII.
    name_keys = {
        "given_name",
        "surname",
        "holder_given_name",
        "holder_surname",
        "passenger_name",
        "full_name",
        "cardholder",
    }

    def walk(obj: Any, key_hint: str | None = None) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, str(k))
            return
        if isinstance(obj, (list, tuple)):
            for v in obj:
                walk(v, key_hint)
            return
        if not isinstance(obj, str):
            return
        if key_hint and key_hint.lower() in name_keys and obj.strip():
            # Skip if the value is already a redaction token.
            if not _TOKEN_RE.fullmatch(obj.strip()):
                raise PIIDetectedError("PII detected: passenger_name")
        kind = _string_has_pii(obj)
        if kind is not None:
            raise PIIDetectedError(f"PII detected: {kind}")

    walk(payload)
