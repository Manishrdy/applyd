"""Role + seniority filtering — title-only matching against curated patterns.

Each role declares positive substring patterns (`include`) and disqualifiers
(`exclude`). Combined into a SQL clause:

    (
      (title LIKE '%backend engineer%' OR title LIKE '%server engineer%' …)
      AND title NOT LIKE '%manager%' AND title NOT LIKE '%director%'
    )

Multiple selected roles OR together. Seniority is a separate cross-cut that
ANDs onto whatever roles (or none) the user picked.

LIKE is case-insensitive by default in SQLite for ASCII, so we don't wrap
the title column in LOWER() — saves a per-row function call and lets any
future title index work.
"""

from __future__ import annotations

from typing import Iterable

# Universal disqualifiers for non-IC roles. Most users searching for an
# "engineer" role do NOT want manager/director matches.
_MANAGER_EXCLUDES: tuple[str, ...] = (
    "manager", "director", "head of", "vp ", "vp,", "vice president",
)

ROLES: dict[str, dict] = {
    "software_engineer": {
        "label": "Software Engineer",
        "include": [
            "software engineer", "software developer", "software dev",
            " swe ", " swe,", "swe -", "programmer", "applications engineer",
        ],
        "exclude": list(_MANAGER_EXCLUDES) + [
            # These have their own buckets — exclude so they don't double-bucket.
            "senior software engineer", "sr software engineer", "sr. software engineer",
            "staff software engineer", "principal software engineer",
            "backend software engineer", "frontend software engineer",
            "data software engineer", "mobile software engineer",
        ],
    },
    "senior_software_engineer": {
        "label": "Senior Software Engineer",
        "include": [
            "senior software engineer", "sr software engineer", "sr. software engineer",
            "senior software developer", "senior swe", "sr swe",
        ],
        "exclude": list(_MANAGER_EXCLUDES) + [
            "staff", "principal", "distinguished",
        ],
    },
    "staff_plus_engineer": {
        "label": "Staff+ Engineer",
        "include": [
            "staff engineer", "staff software engineer", "senior staff engineer",
            "principal engineer", "principal software engineer",
            "distinguished engineer", "distinguished software engineer",
        ],
        "exclude": list(_MANAGER_EXCLUDES),
    },
    "backend_engineer": {
        "label": "Backend Engineer",
        "include": [
            "backend engineer", "back-end engineer", "back end engineer",
            "backend developer", "back-end developer", "back end developer",
            "backend software engineer", "server engineer", "server-side engineer",
            "api engineer", "distributed systems engineer",
            "platform backend engineer",
        ],
        "exclude": list(_MANAGER_EXCLUDES),
    },
    "frontend_engineer": {
        "label": "Frontend Engineer",
        "include": [
            "frontend engineer", "front-end engineer", "front end engineer",
            "frontend developer", "front-end developer", "front end developer",
            "frontend software engineer", "ui engineer", "ui developer",
            "react engineer", "javascript engineer", "web engineer",
            "web developer",
        ],
        "exclude": list(_MANAGER_EXCLUDES),
    },
    "fullstack_engineer": {
        "label": "Full-stack Engineer",
        "include": [
            "full stack engineer", "full-stack engineer", "fullstack engineer",
            "full stack developer", "full-stack developer", "fullstack developer",
            "full stack software engineer", "full-stack software engineer",
        ],
        "exclude": list(_MANAGER_EXCLUDES),
    },
    "ai_ml_engineer": {
        "label": "AI / ML Engineer",
        "include": [
            "ai engineer", "ml engineer", "machine learning engineer",
            "ai/ml engineer", "ml/ai engineer", "deep learning engineer",
            "applied ai engineer", "applied ml engineer", "applied scientist",
            "mlops engineer", "ml platform engineer", "ai platform engineer",
            "ai software engineer", "ml software engineer",
            "generative ai engineer", "llm engineer",
        ],
        "exclude": list(_MANAGER_EXCLUDES) + ["researcher", "research scientist"],
    },
    "forward_deployed_engineer": {
        "label": "Forward Deployed Engineer",
        "include": [
            "forward deployed engineer", "forward-deployed engineer",
            " fde ", " fde,", "fde -", "deployed engineer",
            "deployment engineer", "solutions engineer", "field engineer",
            "implementation engineer",
        ],
        "exclude": list(_MANAGER_EXCLUDES) + ["sales"],
    },
    "founding_engineer": {
        "label": "Founding Engineer",
        "include": [
            "founding engineer", "founding software engineer",
            "founding backend engineer", "founding frontend engineer",
            "founding full stack engineer", "founding full-stack engineer",
            "founding fullstack engineer", "founding ai engineer",
            "founding ml engineer", "founding member of technical staff",
            "founding mts",
        ],
        "exclude": list(_MANAGER_EXCLUDES),
    },
    "product_engineer": {
        "label": "Product Engineer",
        "include": [
            "product engineer", "product software engineer",
        ],
        # The big one — keep product manager / product designer out.
        "exclude": list(_MANAGER_EXCLUDES) + [
            "product manager", "product designer", "product marketing",
            "product owner", "product analyst", "product operations",
            "product support", "product lead", "product specialist",
        ],
    },
    "mobile_engineer": {
        "label": "Mobile Engineer",
        "include": [
            "mobile engineer", "mobile developer", "mobile software engineer",
            "ios engineer", "ios developer", "ios software engineer",
            "android engineer", "android developer", "android software engineer",
            "react native engineer", "swift engineer", "kotlin engineer",
        ],
        "exclude": list(_MANAGER_EXCLUDES),
    },
    "data_engineer": {
        "label": "Data Engineer",
        "include": [
            "data engineer", "analytics engineer",
            "data platform engineer", "data infrastructure engineer",
            "etl engineer", "data pipeline engineer",
        ],
        "exclude": list(_MANAGER_EXCLUDES) + [
            "data scientist", "data analyst", "data architect",
        ],
    },
    "sre_platform_engineer": {
        "label": "SRE / Platform",
        "include": [
            "site reliability engineer", " sre ", " sre,", "sre -",
            "platform engineer", "infrastructure engineer", "devops engineer",
            "production engineer", "cloud engineer", "systems engineer",
            "reliability engineer",
        ],
        "exclude": list(_MANAGER_EXCLUDES) + ["sales engineer"],
    },
    "security_engineer": {
        "label": "Security Engineer",
        "include": [
            "security engineer", "security software engineer",
            "application security engineer", "appsec engineer",
            "infosec engineer", "product security engineer",
            "offensive security engineer", "cloud security engineer",
        ],
        "exclude": list(_MANAGER_EXCLUDES) + ["analyst", "consultant"],
    },
}


SENIORITY: dict[str, dict] = {
    "junior": {
        "label": "Junior",
        "include": [
            "junior ", "jr ", "jr. ", "associate ", "entry level", "entry-level",
            "graduate ", "new grad", "new-grad", "intern", "internship",
            "apprentice",
        ],
    },
    "mid": {
        "label": "Mid",
        # No positive markers — defined as "no level marker present".
        "include": [],
        # Disqualifiers (any of these → NOT mid).
        "exclude_any": [
            "junior ", "jr ", "jr. ", "associate ", "entry level", "entry-level",
            "graduate ", "new grad", "new-grad", "intern", "apprentice",
            "senior ", "sr ", "sr. ", "lead ",
            "staff ", "principal ", "distinguished ",
        ],
    },
    "senior": {
        "label": "Senior",
        "include": ["senior ", "sr ", "sr. ", "lead "],
        "exclude_any": [
            "staff ", "principal ", "distinguished ", "manager", "director",
        ],
    },
    "staff": {
        "label": "Staff",
        "include": ["staff "],
        "exclude_any": ["senior staff "],  # senior staff is principal-ish
    },
    "principal": {
        "label": "Principal+",
        "include": ["principal ", "distinguished ", "senior staff ", "fellow "],
        "exclude_any": [],
    },
}


def _quote_likes(patterns: Iterable[str], col: str = "j.title") -> tuple[str, list]:
    """Build (sql_fragment, params) for `col LIKE ? OR col LIKE ? …`. Each
    pattern is wrapped in % wildcards. Empty patterns iterable returns
    ('1=0', []) — matches nothing, used for guarded SQL composition."""
    pats = list(patterns)
    if not pats:
        return "1=0", []
    placeholders = " OR ".join([f"{col} LIKE ?"] * len(pats))
    params = [f"%{p}%" for p in pats]
    return placeholders, params


def _negative_likes(patterns: Iterable[str], col: str = "j.title") -> tuple[str, list]:
    """Build (sql_fragment, params) for `col NOT LIKE ? AND col NOT LIKE ? …`."""
    pats = list(patterns)
    if not pats:
        return "1=1", []
    placeholders = " AND ".join([f"{col} NOT LIKE ?"] * len(pats))
    params = [f"%{p}%" for p in pats]
    return placeholders, params


def role_clause(role_keys: Iterable[str]) -> tuple[str, list]:
    """OR together the SQL clauses for the selected roles.

    Each role contributes `(include-LIKES) AND (NOT-LIKE excludes)`. Selected
    roles OR together so picking multiple is a union. Returns ('', []) if no
    valid role keys were supplied.
    """
    pieces: list[str] = []
    params: list = []
    for key in role_keys:
        spec = ROLES.get(key)
        if not spec:
            continue
        inc_sql, inc_params = _quote_likes(spec.get("include", []))
        exc_sql, exc_params = _negative_likes(spec.get("exclude", []))
        pieces.append(f"(({inc_sql}) AND ({exc_sql}))")
        params.extend(inc_params)
        params.extend(exc_params)
    if not pieces:
        return "", []
    return "(" + " OR ".join(pieces) + ")", params


def seniority_clause(seniority_keys: Iterable[str]) -> tuple[str, list]:
    """OR together the SQL clauses for the selected seniority levels.

    "Mid" is the negation pattern — title contains no level marker. All other
    levels match by positive substring + a disqualifier list.
    """
    pieces: list[str] = []
    params: list = []
    for key in seniority_keys:
        spec = SENIORITY.get(key)
        if not spec:
            continue
        if spec.get("include"):
            inc_sql, inc_params = _quote_likes(spec["include"])
            params.extend(inc_params)
            parts = [f"({inc_sql})"]
        else:
            parts = []
        if spec.get("exclude_any"):
            exc_sql, exc_params = _negative_likes(spec["exclude_any"])
            params.extend(exc_params)
            parts.append(f"({exc_sql})")
        if not parts:
            continue
        pieces.append("(" + " AND ".join(parts) + ")")
    if not pieces:
        return "", []
    return "(" + " OR ".join(pieces) + ")", params


def list_roles() -> list[dict]:
    """UI-friendly list of (key, label) — preserves dict insertion order."""
    return [{"key": k, "label": v["label"]} for k, v in ROLES.items()]


def list_seniority() -> list[dict]:
    return [{"key": k, "label": v["label"]} for k, v in SENIORITY.items()]
