"""Byte-faithful text file primitives for the mutation path.

Path.write_text() destroys CRLF and read_text(errors="replace") silently
corrupts any byte it cannot decode -- both were doing exactly that in
actions.py. Everything here is strict: decode with plain utf-8 (so a
non-UTF-8 file raises instead of being mangled), keep the file's own EOLs in
`text` rather than normalizing them, and write with newline="" so Python
performs no translation of its own.

Stdlib only, no project imports: a leaf module.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BOM = "\ufeff"
_BOM_BYTES = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class TextFile:
    text: str        # exact decoded content, BOM stripped, EOLs NOT normalized
    eol: str         # dominant EOL: "\r\n" or "\n"
    bom: bool
    mixed_eol: bool


def detect_eol(text: str) -> tuple[str, bool]:
    """(dominant EOL, whether both kinds appear). CRLF wins only on a strict
    majority, so a tie -- or empty/one-line content -- yields ("\\n", ...)."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf == 0 and lf == 0:
        return "\n", False
    return ("\r\n" if crlf > lf else "\n"), (crlf > 0 and lf > 0)


def read(path: Path) -> TextFile:
    """Raises UnicodeDecodeError on a non-UTF-8 file and OSError on an
    unreadable one -- both are refusals the caller must surface, never
    something to paper over with errors="replace"."""
    raw = path.read_bytes()
    bom = raw.startswith(_BOM_BYTES)
    if bom:
        raw = raw[len(_BOM_BYTES):]
    text = raw.decode("utf-8")
    eol, mixed = detect_eol(text)
    return TextFile(text=text, eol=eol, bom=bom, mixed_eol=mixed)


def encode_bytes(text: str, *, bom: bool) -> bytes:
    """Encode to UTF-8 before touching the file so a surrogate or other
    unencodable character cannot truncate an existing target."""
    payload = (BOM if bom else "") + text
    return payload.encode("utf-8")


def write(path: Path, text: str, *, bom: bool) -> None:
    path.write_bytes(encode_bytes(text, bom=bom))


def to_eol(text: str, eol: str) -> str:
    """Normalizes to "\\n" first so mixed input renders consistently."""
    normalized = text.replace("\r\n", "\n")
    return normalized if eol == "\n" else normalized.replace("\n", eol)
