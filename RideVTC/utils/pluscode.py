import re

PLUS_CODE_RE = re.compile(r'^[2-9CFGHJMPQRVWX]{4}\+?[2-9CFGHJMPQRVWX]{2,}(,.*)?$', re.I)

def looks_like_plus_code(s: str | None) -> bool:
    if not s:
        return False
    return bool(PLUS_CODE_RE.match(s.strip()))

PLACEHOLDER_CANDIDATES = {
    'votre position actuelle',
    'ma position',
    'ma position actuelle',
    'current location',
    'ma localisation',
}

def looks_like_placeholder(s: str | None) -> bool:
    if not s:
        return True
    return s.strip().lower() in PLACEHOLDER_CANDIDATES