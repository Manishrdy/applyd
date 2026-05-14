"""URL canonicalization smoke test — assert that the jobhive scraper output
the shim produces uses the same URL canonicalization as the jobhive-published
manifest parquet for the same ATS.

This is the load-bearing assumption behind dedup: `jobs.url` is the natural
key, so a job present in BOTH the manifest cron path AND a manual scrape
must produce IDENTICAL strings — otherwise UPSERT can't recognize them as
the same row and we'd get duplicates.

Integration test: downloads a manifest parquet over the network and spawns
the shim as a subprocess. Skipped by default; opt in with:
  RUN_INTEGRATION_TESTS=1 pytest tests/test_local_scraper_canonicalization.py -v -s
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "vendor" / "ats-scrapers"
SHIM = ROOT / "vendor" / "ats-scrapers-shim" / "scrape_ats.py"
MANIFEST_URL = "https://storage.stapply.ai/jobhive/v1/manifest.json"

INTEGRATION = os.getenv("RUN_INTEGRATION_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not INTEGRATION,
    reason="integration test — set RUN_INTEGRATION_TESTS=1 to run",
)


@pytest.fixture(scope="session")
def lever_manifest_urls(tmp_path_factory) -> set[str]:
    """Download the manifest's lever parquet, return the full URL set.
    Cached for the session — heavy."""
    cache_dir = tmp_path_factory.mktemp("canon")
    with httpx.Client(timeout=180) as c:
        manifest = c.get(MANIFEST_URL).raise_for_status().json()
    parquet_url = manifest["by_ats"]["lever"]["parquet"]
    parquet_path = cache_dir / "lever-manifest.parquet"
    with httpx.Client(timeout=180) as c:
        with c.stream("GET", parquet_url) as r:
            r.raise_for_status()
            with parquet_path.open("wb") as out:
                for chunk in r.iter_bytes(chunk_size=1 << 16):
                    out.write(chunk)
    table = pq.read_table(parquet_path, columns=["url"])
    return set(table.column("url").to_pylist())


def _run_shim_for_company(tmp_path: Path, ats: str, slug: str, name: str = "test") -> Path:
    """Spawn the shim against a single company. Returns the output parquet
    path; the file exists only if the company produced at least one job."""
    out = tmp_path / f"{ats}-{slug}.parquet"
    csv_path = tmp_path / f"{ats}-{slug}.csv"
    with csv_path.open("w") as f:
        w = csv.writer(f)
        w.writerow(["name", "slug", "url"])
        w.writerow([name, slug, f"https://jobs.lever.co/{slug}"])

    cmd = [
        "uv", "run",
        "--directory", str(VENDOR_DIR),
        "--with", "pyarrow",
        "python", str(SHIM),
        ats,
        "--output", str(out),
        "--companies-csv", str(csv_path),
        "--concurrency", "1",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    if proc.returncode != 0:
        print("=== shim stderr ===")
        print(proc.stderr.decode("utf-8", errors="replace"))
    assert proc.returncode == 0, f"shim exited {proc.returncode} for {ats}/{slug}"
    return out


def _slugs_from_manifest_urls(manifest_urls: set[str], n: int) -> list[str]:
    """Extract n distinct slugs from manifest URLs, prioritizing companies
    that have multiple jobs (more material for the comparison)."""
    from collections import Counter
    slug_counts: Counter[str] = Counter()
    for u in manifest_urls:
        # https://jobs.lever.co/{slug}/{uuid}
        parts = u.split("/")
        if len(parts) >= 4 and parts[2] == "jobs.lever.co":
            slug_counts[parts[3]] += 1
    # Take the top-n by job count — bigger boards = more chance of overlap
    return [s for s, _ in slug_counts.most_common(n)]


def test_lever_url_canonicalization(tmp_path, lever_manifest_urls):
    """Sample 5 companies that appear in the manifest. For each, scrape via
    the shim and compute URL intersection with the manifest's URLs for that
    same company. PASS if at least one sampled company has non-empty
    intersection. FAIL with diagnostics if all five have empty intersection
    despite both sets being non-empty (that's canonicalization drift)."""
    slugs = _slugs_from_manifest_urls(lever_manifest_urls, n=5)
    assert slugs, "no lever slugs in manifest"

    results = []
    for slug in slugs:
        out = _run_shim_for_company(tmp_path, "lever", slug)
        if not out.exists():
            results.append((slug, 0, 0, 0, "shim produced no parquet"))
            continue
        shim_urls = set(pq.read_table(out, columns=["url"]).column("url").to_pylist())
        prefix = f"https://jobs.lever.co/{slug}/"
        manifest_urls = {u for u in lever_manifest_urls if u.startswith(prefix)}
        overlap = shim_urls & manifest_urls
        results.append((slug, len(shim_urls), len(manifest_urls), len(overlap),
                        None if overlap or not shim_urls or not manifest_urls
                        else "EMPTY OVERLAP — possible drift"))

    print()
    print(f"{'slug':<22} {'shim':>6} {'manifest':>9} {'overlap':>8}  note")
    print("-" * 70)
    for slug, s, m, o, note in results:
        print(f"{slug:<22} {s:>6} {m:>9} {o:>8}  {note or 'OK'}")

    any_overlap = any(o > 0 for _slug, _s, _m, o, _ in results)
    if not any_overlap:
        # Worst case: every sampled company had non-empty shim AND non-empty
        # manifest but zero intersection. That's URL drift.
        drift = [r for r in results if r[1] > 0 and r[2] > 0]
        if drift:
            samples = []
            for slug, _, _, _, _ in drift[:2]:
                out = tmp_path / f"lever-{slug}.parquet"
                shim_sample = list(pq.read_table(out, columns=["url"]).column("url").to_pylist())[:1]
                manifest_sample = [u for u in lever_manifest_urls
                                    if u.startswith(f"https://jobs.lever.co/{slug}/")][:1]
                samples.append(f"  {slug}:  shim={shim_sample}  manifest={manifest_sample}")
            pytest.fail(
                "URL canonicalization drift suspected — both shim and manifest "
                "produced URLs for the same companies but ZERO matched exactly. "
                "Examples:\n" + "\n".join(samples)
            )
        pytest.skip(
            "no overlap measurable: every sampled company had either an "
            "empty shim result or no manifest entries. Can't assert."
        )
    # At least one company had real overlap — canonicalization matches.
