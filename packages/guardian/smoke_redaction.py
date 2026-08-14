"""Smoke: Guardian redaction round-trip, assert_no_pii, EXIF GPS strip (Task 8)."""

from __future__ import annotations

import struct
import sys
import traceback
from datetime import UTC, datetime

from packages.atlas.models import Passenger
from packages.guardian.redaction import (
    assert_no_pii,
    redact,
    redact_image_metadata,
    rehydrate,
)

TEST_PAN = "4111111111111111"  # well-known Luhn-valid Visa test PAN


def _build_exif_gps_jpeg() -> bytes:
    """Minimal JPEG with an APP1 Exif segment that contains a GPS IFD."""
    # TIFF (little-endian) with IFD0 pointing at a GPS IFD via tag 0x8825.
    # GPS IFD has GPSLatitudeRef (1) = "N".
    tiff = bytearray()
    tiff += b"II"  # little-endian
    tiff += struct.pack("<H", 42)
    tiff += struct.pack("<I", 8)  # offset to first IFD

    # IFD0: 1 entry (GPSOffset) + next-IFD=0 + GPS IFD body follows.
    gps_ifd_offset = 8 + 2 + 12 + 4  # after IFD0 header/entry/next
    ifd0 = bytearray()
    ifd0 += struct.pack("<H", 1)
    # tag 0x8825 GPSOffset, type LONG (4), count 1, value=offset
    ifd0 += struct.pack("<HHII", 0x8825, 4, 1, gps_ifd_offset)
    ifd0 += struct.pack("<I", 0)  # next IFD

    gps_ifd = bytearray()
    gps_ifd += struct.pack("<H", 1)
    # tag 1 GPSLatitudeRef, type ASCII (2), count 2, value inline "N\0\0\0"
    gps_ifd += struct.pack("<HHII", 1, 2, 2, 0x0000004E)
    gps_ifd += struct.pack("<I", 0)

    tiff += ifd0
    tiff += gps_ifd

    exif_payload = b"Exif\x00\x00" + bytes(tiff)
    app1_len = 2 + len(exif_payload)
    app1 = b"\xff\xe1" + struct.pack(">H", app1_len) + exif_payload

    # Minimal JFIF APP0 + empty image terminator so the file is SOI…EOI shaped.
    app0 = (
        b"\xff\xe0"
        + struct.pack(">H", 16)
        + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    )
    # 1x1 greyscale SOF0 + tiny Huffman/scan so pixel payload exists.
    # Use a known-valid tiny JPEG body and splice APP1 after SOI.
    tiny = bytes(
        [
            0xFF,
            0xD8,
            0xFF,
            0xDB,
            0x00,
            0x43,
            0x00,
            *[0x10] * 64,
            0xFF,
            0xC0,
            0x00,
            0x0B,
            0x08,
            0x00,
            0x01,
            0x00,
            0x01,
            0x01,
            0x01,
            0x11,
            0x00,
            0xFF,
            0xC4,
            0x00,
            0x14,
            0x00,
            0x01,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0x00,
            0xFF,
            0xDA,
            0x00,
            0x08,
            0x01,
            0x01,
            0x00,
            0x00,
            0x3F,
            0x00,
            0x7F,
            0xFF,
            0xD9,
        ]
    )
    # SOI + APP1(Exif/GPS) + APP0 + rest-after-SOI
    return b"\xff\xd8" + app1 + app0 + tiny[2:]


def _exif_dump(label: str, data: bytes) -> None:
    has_app1 = b"\xff\xe1" in data
    has_exif = b"Exif\x00\x00" in data
    has_gps_tag = b"\x25\x88" in data or b"\x88\x25" in data  # GPSOffset LE/BE
    # ASCII 'N' latitude ref inside GPS IFD is weak; also search for GPS marker text.
    has_gps_ascii = b"GPS" in data
    print(f"{label}: bytes={len(data)} APP1={has_app1} Exif={has_exif} "
          f"GPSOffset_tag={has_gps_tag} GPS_ascii={has_gps_ascii}")
    # Hex window around first APP1 if present.
    idx = data.find(b"\xff\xe1")
    if idx >= 0:
        window = data[idx : idx + min(64, len(data) - idx)]
        print(f"{label} APP1 head: {window.hex()}")
    else:
        print(f"{label} APP1 head: <none>")


def main() -> int:
    pax1 = Passenger(
        given_name="Mei Ling",
        surname="Tan",
        date_of_birth=datetime(1952, 3, 14, tzinfo=UTC),
        passport_number="A12345678",
        nationality="SG",
    )
    pax2 = Passenger(
        given_name="Wei",
        surname="Chen",
        date_of_birth=datetime(1988, 7, 22, tzinfo=UTC),
        passport_number="B98765432",
        nationality="SG",
    )

    raw = (
        "Travellers Mei Ling Tan and Wei Chen need rebooking. "
        "Passports A12345678 and B98765432. "
        "DOB 1952-03-14. "
        f"Card {TEST_PAN}."
    )
    print("=== BEFORE (raw) ===")
    print(raw)

    result = redact(raw, passengers=[pax1, pax2])
    print("=== AFTER (redacted) ===")
    print(result.text)
    print("kinds_found:", result.kinds_found)
    # Map keys only — never dump values in ordinary logs; smoke may show tokens.
    print("tokens:", sorted(result.map.tokens.keys()))

    restored = rehydrate(result.text, result.map)
    print("=== REHYDRATED ===")
    print(restored)
    if restored != raw:
        print("FAIL: round-trip fidelity broken", file=sys.stderr)
        print(f"  expected: {raw!r}", file=sys.stderr)
        print(f"  got:      {restored!r}", file=sys.stderr)
        return 1
    print("round-trip OK: rehydrate(redact(text).text, map) == text")

    # Determinism check.
    again = redact(raw, passengers=[pax1, pax2])
    if again.text != result.text or again.map.tokens != result.map.tokens:
        print("FAIL: non-deterministic redaction", file=sys.stderr)
        return 1
    print("determinism OK")

    print("=== assert_no_pii on RAW ===")
    raised = False
    try:
        assert_no_pii({"text": raw})
    except Exception as exc:
        raised = True
        print(f"raised {type(exc).__name__}: {exc}")
        traceback.print_exc()
    if not raised:
        print("FAIL: assert_no_pii did not raise on raw text", file=sys.stderr)
        return 1

    print("=== assert_no_pii on REDACTED ===")
    assert_no_pii({"text": result.text})
    print("assert_no_pii passed on redacted text")

    print("=== EXIF GPS strip ===")
    jpeg = _build_exif_gps_jpeg()
    _exif_dump("BEFORE", jpeg)
    clean = redact_image_metadata(jpeg)
    _exif_dump("AFTER", clean)
    if b"\xff\xe1" in clean or b"Exif\x00\x00" in clean:
        print("FAIL: EXIF/APP1 survived redaction", file=sys.stderr)
        return 1
    if b"\x25\x88" in clean:  # little-endian GPSOffset tag bytes
        # Could false-positive in compressed data; still flag for this crafted fixture.
        print("FAIL: GPSOffset tag bytes survived", file=sys.stderr)
        return 1
    if not clean.startswith(b"\xff\xd8"):
        print("FAIL: output is not JPEG SOI", file=sys.stderr)
        return 1
    print("EXIF GPS strip OK")

    # PAN detection without passenger list.
    pan_only = redact(f"pay with {TEST_PAN}")
    if TEST_PAN in pan_only.text or "pan" not in pan_only.kinds_found:
        print("FAIL: PAN not redacted without passengers", file=sys.stderr)
        return 1
    print("PAN-without-passengers OK:", pan_only.text)

    print("OK: smoke_redaction passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
