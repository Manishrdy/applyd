"""Ingestion pipeline: download per-ATS parquet, stream-parse, upsert into SQLite, prune.

Flow per cycle:
  1. fetch manifest
  2. short-circuit if manifest.updated_at == last successful manifest_log entry
  3. download every ATS parquet in parallel (sha256 cache-skip)
  4. stream each parquet in batches → filter to last N days by posted_at →
     normalize/strip → ON CONFLICT(url) DO UPDATE
  5. DELETE rows older than rolling_window_days
  6. rebuild + optimize FTS5
  7. log row to manifest_log
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
import pandas as pd
import pyarrow.parquet as pq

from app.config import settings
from app.database import get_db, optimize_fts, rebuild_fts
from app.services import manifest as manifest_svc

log = logging.getLogger(__name__)

# Country detection patterns — order matters (cheaper checks first).
# Tradeoffs: cities by bare name is the noisiest signal (false positives for
# overseas namesakes like "Portland, Australia") but is needed because many
# ATS sources expose location as just the city (e.g. Apple: "Sunnyvale").
# We exclude rows where location explicitly contains a non-US country.

_NON_US_COUNTRY_RE = re.compile(
    r"\b(United Kingdom|UK|England|Scotland|Wales|Ireland|"
    r"Germany|France|Spain|Italy|Netherlands|Belgium|Switzerland|Austria|"
    r"Sweden|Norway|Denmark|Finland|Poland|Portugal|Greece|Czechia|Romania|"
    r"India|China|Japan|Korea|Singapore|Hong Kong|Taiwan|Thailand|Vietnam|"
    r"Malaysia|Indonesia|Philippines|Pakistan|Bangladesh|Sri Lanka|"
    r"Australia|New Zealand|Brazil|Mexico|Argentina|Chile|Colombia|Peru|"
    r"Canada|Toronto|Vancouver|Montreal|Calgary|Ottawa|"
    r"London|Paris|Berlin|Munich|Amsterdam|Dublin|Madrid|Barcelona|Rome|"
    r"Stockholm|Oslo|Helsinki|Warsaw|Lisbon|Athens|Prague|Bucharest|"
    r"Mumbai|Bangalore|Bengaluru|Delhi|Hyderabad|Pune|Chennai|Gurgaon|Noida|"
    r"Beijing|Shanghai|Shenzhen|Tokyo|Osaka|Seoul|Sydney|Melbourne|"
    r"São Paulo|Sao Paulo|Mexico City|Buenos Aires|Tel Aviv|Dubai|UAE|"
    r"Remote - EMEA|Remote - APAC|Remote - EU)\b",
    re.IGNORECASE,
)

_USA_EXPLICIT_RE = re.compile(
    r"\b(?:United States(?:\s+of\s+America)?|U\.S\.A?\.?|USA|US)\b",
    re.IGNORECASE,
)

# Three-part locations like "Ahmedabad, GJ, IN" or "Jakarta, JK, ID" use the
# trailing 2-letter token as ISO country code, not US state code. Without
# this filter, "IN" (India) matches Indiana, "ID" (Indonesia) matches Idaho,
# etc. Run BEFORE the state-code regex.
_NON_US_TRAILING_CC_RE = re.compile(
    r",\s*[A-Z][A-Za-z]{0,4},\s*(?:IN|ID|DE|FR|GB|UK|IT|ES|NL|BE|CH|AT|SE|"
    r"NO|DK|FI|PL|PT|CZ|GR|RO|IE|IL|AE|SG|HK|TW|TH|VN|MY|PH|JP|KR|CN|AU|"
    r"NZ|BR|MX|AR|CL|CO|PE|ZA|EG|TR|RU|UA|CA|PK|BD|LK|EE|LV|LT|HU|SK|SI|"
    r"HR|BG|RS|BA|MK|MD|BY|GE|AM|AZ|KZ|UZ|MN)\s*$"
)

# 2-letter state codes require comma context (e.g. "Wilmington, DE") so we
# don't false-positive on bare ISO country codes like "DE" (Germany).
_USA_STATE_CODE_RE = re.compile(
    r",\s*("
    r"AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|"
    r"MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|"
    r"TX|UT|VT|VA|WA|WV|WI|WY|DC"
    r")\b"
)

# Full state names are unambiguous enough to match anywhere.
_USA_STATE_NAME_RE = re.compile(
    r"\b("
    r"Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|"
    r"Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|"
    r"Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|"
    r"Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|"
    r"New Mexico|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|"
    r"Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|"
    r"Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming"
    r")\b",
    re.IGNORECASE,
)

# Curated list of US cities/metros that frequently appear bare (no state
# suffix). Order longest-first within alternatives to avoid prefix shadowing.
_USA_CITY_RE = re.compile(
    r"\b("
    r"New York City|New York|Los Angeles|San Francisco|San Jose|San Diego|"
    r"San Antonio|San Mateo|Santa Clara|Santa Monica|Santa Ana|Santa Cruz|"
    r"Santa Rosa|Santa Clarita|Saint Louis|St\. Louis|St\. Paul|Saint Paul|"
    r"St\. Petersburg|Saint Petersburg|Salt Lake City|Kansas City|"
    r"Oklahoma City|Jersey City|Long Beach|Virginia Beach|Newport Beach|"
    r"Huntington Beach|Pembroke Pines|Cape Coral|Coral Springs|Fort Worth|"
    r"Fort Lauderdale|Fort Wayne|Fort Collins|Grand Rapids|Grand Prairie|"
    r"Round Rock|Cedar Rapids|Sioux Falls|Newport News|Rancho Cucamonga|"
    r"Overland Park|West Valley City|North Las Vegas|North Charleston|"
    r"Winston-Salem|Sterling Heights|College Station|Lake Forest|Walnut Creek|"
    r"Mountain View|Menlo Park|Foster City|Daly City|Redwood City|"
    r"Culver City|Beverly Hills|Costa Mesa|Newport Beach|Palo Alto|Cupertino|"
    r"Sunnyvale|Pleasanton|Berkeley|Oakland|Fremont|Hayward|Burbank|Pasadena|"
    r"Glendale|Inglewood|Anaheim|Irvine|Riverside|Bakersfield|Fresno|Stockton|"
    r"Modesto|Sacramento|Roseville|Folsom|Davis|Vallejo|Concord|Antioch|"
    r"Richmond|Salinas|Monterey|Carlsbad|Escondido|El Cajon|Chula Vista|"
    r"Oceanside|Temecula|Murrieta|Lancaster|Palmdale|Victorville|Pomona|"
    r"Rialto|Fontana|Ontario|Corona|Norwalk|Downey|Compton|El Monte|Torrance|"
    r"Ventura|Oxnard|Thousand Oaks|Simi Valley|Camarillo|Visalia|Clovis|"
    r"Seattle|Bellevue|Redmond|Kirkland|Tacoma|Spokane|Olympia|Vancouver|"
    r"Portland|Beaverton|Hillsboro|Gresham|Salem|Eugene|Bend|"
    r"Boise|Meridian|Reno|Henderson|Las Vegas|Sparks|"
    r"Phoenix|Mesa|Tucson|Chandler|Scottsdale|Tempe|Gilbert|Glendale|Peoria|"
    r"Surprise|Yuma|Albuquerque|Santa Fe|"
    r"Denver|Aurora|Boulder|Colorado Springs|Fort Collins|Lakewood|Thornton|"
    r"Arvada|Westminster|Pueblo|Centennial|"
    r"Austin|Dallas|Houston|Plano|Frisco|McKinney|Irving|Garland|Arlington|"
    r"Mesquite|Carrollton|Lewisville|Denton|Richardson|Allen|Round Rock|"
    r"Pearland|Sugar Land|Beaumont|Waco|Lubbock|Amarillo|El Paso|Killeen|"
    r"Midland|Odessa|Abilene|Brownsville|Laredo|McAllen|Corpus Christi|"
    r"Tyler|College Station|Conroe|"
    r"Atlanta|Sandy Springs|Roswell|Marietta|Alpharetta|Athens|Augusta|"
    r"Columbus|Savannah|Macon|"
    r"Miami|Tampa|Orlando|Jacksonville|Tallahassee|Hialeah|"
    r"Hollywood|Gainesville|Pompano Beach|West Palm Beach|Port St\. Lucie|"
    r"Cape Coral|Fort Myers|Sarasota|Clearwater|Lakeland|Pensacola|"
    r"Charlotte|Raleigh|Durham|Greensboro|Cary|Wilmington|Asheville|"
    r"Fayetteville|High Point|Chapel Hill|"
    r"Nashville|Memphis|Knoxville|Chattanooga|Murfreesboro|Clarksville|"
    r"Louisville|Lexington|"
    r"Indianapolis|Fort Wayne|Evansville|South Bend|Bloomington|Carmel|Fishers|"
    r"Columbus|Cleveland|Cincinnati|Akron|Toledo|Dayton|"
    r"Chicago|Naperville|Aurora|Joliet|Rockford|Elgin|Peoria|Springfield|"
    r"Champaign|Schaumburg|Evanston|Oak Park|"
    r"Detroit|Grand Rapids|Ann Arbor|Lansing|Warren|Sterling Heights|Troy|"
    r"Flint|Dearborn|Livonia|"
    r"Milwaukee|Madison|Green Bay|Kenosha|Racine|Appleton|"
    r"Minneapolis|Saint Paul|St\. Paul|Bloomington|Rochester|Duluth|"
    r"Saint Cloud|St\. Cloud|"
    r"Kansas City|Saint Louis|St\. Louis|Springfield|Independence|"
    r"Omaha|Lincoln|Bellevue|"
    r"Oklahoma City|Tulsa|Norman|Broken Arrow|"
    r"Little Rock|Fayetteville|Fort Smith|Bentonville|"
    r"New Orleans|Baton Rouge|Shreveport|Lafayette|Lake Charles|"
    r"Jackson|Birmingham|Montgomery|Mobile|Huntsville|Tuscaloosa|"
    r"Boston|Cambridge|Worcester|Springfield|Lowell|Quincy|Brockton|Newton|"
    r"Somerville|Framingham|Waltham|"
    r"Providence|Warwick|Cranston|"
    r"Hartford|New Haven|Stamford|Bridgeport|Waterbury|Norwalk|Greenwich|"
    r"Burlington|Manchester|Concord|Nashua|"
    r"Portland|Bangor|"
    r"New York|Manhattan|Brooklyn|Queens|Bronx|Staten Island|Yonkers|Rochester|"
    r"Buffalo|Syracuse|Albany|White Plains|New Rochelle|"
    r"Newark|Jersey City|Paterson|Elizabeth|Edison|Trenton|Hoboken|"
    r"Philadelphia|Pittsburgh|Allentown|Erie|Reading|Bethlehem|Scranton|"
    r"Lancaster|Harrisburg|King of Prussia|"
    r"Baltimore|Annapolis|Frederick|Rockville|Gaithersburg|Bethesda|"
    r"Silver Spring|"
    r"Washington|Washington DC|Arlington|Alexandria|Reston|Fairfax|Tysons|"
    r"Herndon|McLean|Vienna|"
    r"Richmond|Virginia Beach|Norfolk|Chesapeake|Newport News|Hampton|"
    r"Roanoke|Lynchburg|Charlottesville|"
    r"Charleston|Huntington|Morgantown|"
    r"Wilmington|Newark|Dover|"
    r"Honolulu|Pearl City|Hilo|"
    r"Anchorage|Fairbanks|Juneau"
    r")\b",
    re.IGNORECASE,
)

USA_PATTERNS = [_USA_EXPLICIT_RE, _USA_STATE_CODE_RE, _USA_STATE_NAME_RE, _USA_CITY_RE]

PERIOD_MULTIPLIER = {
    "HOUR": 2080, "HOURLY": 2080,
    "WEEK": 52, "WEEKLY": 52,
    "MONTH": 12, "MONTHLY": 12,
    "YEAR": 1, "YEARLY": 1, "ANNUAL": 1, "ANNUALLY": 1,
}

JOB_COLUMNS = [
    "url", "title", "company", "ats_type", "ats_id", "location", "is_remote",
    "salary_min", "salary_max", "salary_currency", "salary_period",
    "salary_summary", "employment_type", "department", "team", "description",
    "posted_at", "requisition_id", "apply_url", "commitment",
]

_UPSERT_COLUMNS = JOB_COLUMNS + [
    "country", "salary_min_usd_annual", "salary_max_usd_annual", "fetched_cycle",
]
_UPDATE_SET = ",\n    ".join(
    f"{c}=excluded.{c}" for c in _UPSERT_COLUMNS if c != "url"
)
UPSERT_SQL = f"""
INSERT INTO jobs ({', '.join(_UPSERT_COLUMNS)})
VALUES ({', '.join(['?'] * len(_UPSERT_COLUMNS))})
ON CONFLICT(url) DO UPDATE SET
    {_UPDATE_SET},
    updated_at=datetime('now')
"""

_HTML_BLOCK_RE = re.compile(r"</?(p|div|br|li|tr|h[1-6])[^>]*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&apos;": "'",
}
_MULTI_SPACE_RE = re.compile(r"[ \t]+")
_MULTI_NEWLINE_RE = re.compile(r"\n\s*\n+")


def strip_html(text: Any) -> str | None:
    """Fast regex-based HTML strip. Loses bold/links but preserves paragraph breaks.

    BS4 is ~10× slower per row; for millions of rows the regex approach is the only
    reasonable bulk-strip path.
    """
    if not isinstance(text, str):
        return None
    if not text.strip():
        return ""
    if "<" not in text:
        return text.strip()
    s = _HTML_BLOCK_RE.sub("\n", text)
    s = _HTML_TAG_RE.sub("", s)
    for ent, rep in _HTML_ENTITIES.items():
        s = s.replace(ent, rep)
    s = _MULTI_SPACE_RE.sub(" ", s)
    s = _MULTI_NEWLINE_RE.sub("\n\n", s)
    return s.strip()


def extract_country(location: Any) -> str | None:
    if not isinstance(location, str) or not location.strip():
        return None
    # Negative filters first: if location explicitly names a non-US
    # country/major city OR ends with a non-US ISO country code in the
    # 3-part "City, Region, CC" pattern, refuse to tag as US.
    if _NON_US_COUNTRY_RE.search(location):
        return None
    if _NON_US_TRAILING_CC_RE.search(location):
        return None
    for pat in USA_PATTERNS:
        if pat.search(location):
            return "US"
    return None


def _is_missing(v: Any) -> bool:
    """True if a value is None/NaN/NaT/pd.NA."""
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _to_float(v: Any) -> float | None:
    if _is_missing(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_str(v: Any) -> str | None:
    """Coerce to a string, mapping NaN/NaT/None to SQL NULL."""
    if _is_missing(v):
        return None
    if isinstance(v, str):
        return v
    return str(v)


def _to_iso(v: Any) -> str | None:
    """Coerce a timestamp-ish value to ISO8601 string, or None if missing."""
    if _is_missing(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, str):
        return v
    return str(v)


def normalize_salary(
    minimum: Any, maximum: Any, currency: Any, period: Any
) -> tuple[float | None, float | None]:
    if currency not in (None, "USD"):
        return (None, None)
    if not isinstance(period, str):
        return (None, None)
    mult = PERIOD_MULTIPLIER.get(period.upper())
    if mult is None:
        return (None, None)
    mn = _to_float(minimum)
    mx = _to_float(maximum)
    return (
        mn * mult if mn is not None else None,
        mx * mult if mx is not None else None,
    )


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def download_parquet(
    client: httpx.AsyncClient,
    ats: str,
    meta: dict[str, Any],
    cache_dir: Path,
) -> Path:
    url = meta["parquet"]
    expected_sha = meta.get("parquet_sha256")
    dest = cache_dir / f"{ats}.parquet"

    if dest.exists() and expected_sha:
        if file_sha256(dest) == expected_sha:
            log.info("[%s] cache hit (sha256 match), skipping download", ats)
            return dest

    t0 = time.time()
    log.info("[%s] downloading %s", ats, url)
    async with client.stream("GET", url) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            async for chunk in r.aiter_bytes(chunk_size=1 << 20):
                f.write(chunk)

    if expected_sha and file_sha256(dest) != expected_sha:
        dest.unlink()
        raise RuntimeError(f"[{ats}] sha256 mismatch after download")
    size_mb = dest.stat().st_size / 1e6
    log.info("[%s] downloaded %.1f MB in %.1fs", ats, size_mb, time.time() - t0)
    return dest


def _row_to_tuple(row: dict, ats_default: str, fetched_cycle: str) -> tuple:
    is_remote_raw = row.get("is_remote")
    if _is_missing(is_remote_raw):
        is_remote = None
    else:
        try:
            is_remote = 1 if bool(is_remote_raw) else 0
        except (TypeError, ValueError):
            is_remote = None

    ats_type = _to_str(row.get("ats_type")) or ats_default
    salary_currency = _to_str(row.get("salary_currency"))
    salary_period = _to_str(row.get("salary_period"))
    location = _to_str(row.get("location"))
    smin_usd, smax_usd = normalize_salary(
        row.get("salary_min"), row.get("salary_max"),
        salary_currency, salary_period,
    )
    description = _to_str(row.get("description"))
    if description is not None:
        description = strip_html(description)

    return (
        _to_str(row.get("url")),
        _to_str(row.get("title")),
        _to_str(row.get("company")),
        ats_type,
        _to_str(row.get("ats_id")),
        location,
        is_remote,
        _to_float(row.get("salary_min")),
        _to_float(row.get("salary_max")),
        salary_currency,
        salary_period,
        _to_str(row.get("salary_summary")),
        _to_str(row.get("employment_type")),
        _to_str(row.get("department")),
        _to_str(row.get("team")),
        description,
        _to_iso(row.get("posted_at")),
        _to_str(row.get("requisition_id")),
        _to_str(row.get("apply_url")),
        _to_str(row.get("commitment")),
        extract_country(location),
        smin_usd,
        smax_usd,
        fetched_cycle,
    )


def process_parquet(
    parquet_path: Path,
    ats: str,
    conn: sqlite3.Connection,
    cutoff_ts: pd.Timestamp,
    fetched_cycle: str,
    batch_size: int,
) -> tuple[int, int]:
    """Stream a parquet, filter to last N days, upsert. Returns (seen, upserted)."""
    pf = pq.ParquetFile(str(parquet_path))
    schema_cols = set(pf.schema_arrow.names)
    cols_to_read = [c for c in JOB_COLUMNS if c in schema_cols]
    if "url" not in cols_to_read or "posted_at" not in cols_to_read:
        log.warning("[%s] parquet missing url or posted_at column, skipping", ats)
        return 0, 0

    total_seen = 0
    total_upserted = 0

    for batch in pf.iter_batches(batch_size=batch_size, columns=cols_to_read):
        df = batch.to_pandas()
        total_seen += len(df)

        ts = pd.to_datetime(df["posted_at"], errors="coerce", utc=True)
        # Keep rows posted within the rolling window, AND undated rows
        # (NULL posted_at). Many ATS sources — Workday, SuccessFactors,
        # Google, Meta, Tesla, TikTok — don't expose post date. We treat
        # those as "currently active" (the upstream is a live snapshot) and
        # prune them via fetched_cycle staleness instead.
        mask = ts.isna() | (ts >= cutoff_ts)
        df = df.loc[mask]
        if df.empty:
            continue

        df = df[df["url"].notna() & (df["url"].astype(str).str.len() > 0)]
        if df.empty:
            continue

        rows = [_row_to_tuple(r, ats, fetched_cycle) for r in df.to_dict("records")]
        conn.execute("BEGIN")
        try:
            conn.executemany(UPSERT_SQL, rows)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        total_upserted += len(rows)

    return total_seen, total_upserted


def prune_old(conn: sqlite3.Connection, days: int, current_cycle: str) -> int:
    """Drop any row whose effective date (COALESCE(posted_at, first_seen_at))
    is older than the rolling window. This handles dated and undated rows
    uniformly: dated rows age out by upstream timestamp; undated rows age out
    relative to when we first observed them.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cur = conn.execute(
        "DELETE FROM jobs WHERE COALESCE(posted_at, first_seen_at) < ?",
        (cutoff,),
    )
    return cur.rowcount


async def _download_all(
    manifest: dict[str, Any],
    ats_names: list[str],
) -> list[tuple[str, Path | None, str | None]]:
    sem = asyncio.Semaphore(settings.download_concurrency)

    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
        async def _one(ats: str) -> tuple[str, Path | None, str | None]:
            meta = manifest_svc.ats_meta(manifest, ats)
            if not meta or not meta.get("parquet"):
                return ats, None, "no parquet URL in manifest"
            async with sem:
                try:
                    p = await download_parquet(client, ats, meta, settings.cache_dir)
                    return ats, p, None
                except Exception as e:
                    log.exception("[%s] download failed", ats)
                    return ats, None, str(e)

        return await asyncio.gather(*[_one(a) for a in ats_names])


async def run_ingestion(
    ats_filter: Iterable[str] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Run a full ingestion cycle.

    ats_filter: limit to these ATS names (for verification gates).
    force: bypass the manifest_log.updated_at short-circuit.
    """
    t0 = time.time()
    fetched_at = datetime.now(timezone.utc).isoformat()
    fetched_cycle = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cutoff_ts = pd.Timestamp(
        datetime.now(timezone.utc) - timedelta(days=settings.rolling_window_days)
    )

    log.info("fetching manifest from %s", settings.manifest_url)
    manifest = await manifest_svc.fetch_manifest()
    manifest_updated_at = manifest.get("updated_at", "")
    total_jobs_upstream = (
        (manifest.get("stats") or {}).get("total_jobs")
        or manifest.get("total_jobs")
    )

    with get_db() as conn:
        if not force and not manifest_svc.should_ingest(conn, manifest):
            log.info("manifest unchanged (updated_at=%s); skipping",
                     manifest_updated_at)
            conn.execute(
                "INSERT INTO manifest_log (fetched_at, manifest_updated_at, "
                "total_jobs_upstream, ats_count, rows_ingested, rows_pruned, "
                "status, duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fetched_at, manifest_updated_at, total_jobs_upstream,
                 len(manifest_svc.list_ats(manifest)), 0, 0, "skipped",
                 time.time() - t0),
            )
            return {"status": "skipped", "manifest_updated_at": manifest_updated_at}

    ats_names = manifest_svc.list_ats(manifest)
    if ats_filter is not None:
        f = {a.lower() for a in ats_filter}
        ats_names = [a for a in ats_names if a.lower() in f]
    log.info("ingesting %d ATS sources", len(ats_names))

    download_results = await _download_all(manifest, ats_names)

    per_ats: dict[str, dict[str, Any]] = {}
    total_upserted = 0
    error: str | None = None

    try:
        with get_db() as conn:
            for ats, path, err in download_results:
                if path is None:
                    per_ats[ats] = {"status": "failed", "error": err, "seen": 0, "upserted": 0}
                    continue
                try:
                    seen, upserted = process_parquet(
                        path, ats, conn, cutoff_ts, fetched_cycle,
                        settings.ingest_batch_size,
                    )
                    per_ats[ats] = {"status": "ok", "seen": seen, "upserted": upserted}
                    total_upserted += upserted
                    log.info("[%s] seen=%d upserted=%d", ats, seen, upserted)
                except Exception as e:
                    log.exception("[%s] process failed", ats)
                    per_ats[ats] = {"status": "failed", "error": str(e), "seen": 0, "upserted": 0}

            rows_pruned = prune_old(conn, settings.rolling_window_days, fetched_cycle)
            log.info("pruned %d rows (old-dated + stale-undated)", rows_pruned)

            log.info("rebuilding FTS5 index")
            rebuild_fts(conn)
            optimize_fts(conn)

            duration = time.time() - t0
            conn.execute(
                "INSERT INTO manifest_log (fetched_at, manifest_updated_at, "
                "total_jobs_upstream, ats_count, rows_ingested, rows_pruned, "
                "status, duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fetched_at, manifest_updated_at, total_jobs_upstream,
                 len(ats_names), total_upserted, rows_pruned, "success",
                 duration),
            )
    except Exception as e:
        error = str(e)
        log.exception("ingestion failed")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO manifest_log (fetched_at, manifest_updated_at, "
                "total_jobs_upstream, ats_count, rows_ingested, rows_pruned, "
                "status, error, duration_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (fetched_at, manifest_updated_at, total_jobs_upstream,
                 len(ats_names), total_upserted, 0, "failed", error,
                 time.time() - t0),
            )
        raise

    return {
        "status": "success",
        "duration_seconds": duration,
        "rows_upserted": total_upserted,
        "rows_pruned": rows_pruned,
        "manifest_updated_at": manifest_updated_at,
        "per_ats": per_ats,
    }
