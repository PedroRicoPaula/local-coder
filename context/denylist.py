"""Files refused as context, even inside the project root.

Ported from the Context-Compress-Engine's own denylist.rs (see that repo's
git history, 2026-08-24 audit): prefix families, not exact names, and
case-insensitive matching. An exact-name list lets `.env.local` (the file
that usually holds live credentials in Next.js/Vite/CRA, while `.env` itself
is often a committed placeholder) or `id_ecdsa` through while blocking only
`.env` / `id_rsa`. A deny list is mitigation, not a promise -- it does not
replace judgment about what you paste into a prompt yourself.
"""
from __future__ import annotations

from pathlib import Path

DENIED_NAMES = {
    ".netrc", ".npmrc", ".pgpass", "credentials",
    ".htpasswd", "secrets.yaml", "secrets.yml",
}

DENIED_PREFIXES = (".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")

DENIED_EXTENSIONS = {
    "pem", "key", "p12", "pfx", "keystore", "jks", "kdbx", "ppk", "asc",
}


def is_denied(path: str) -> bool:
    p = Path(path)
    name_lower = p.name.lower()
    if name_lower in DENIED_NAMES or any(name_lower.startswith(pre) for pre in DENIED_PREFIXES):
        return True
    ext = p.suffix.lstrip(".").lower()
    return ext in DENIED_EXTENSIONS
