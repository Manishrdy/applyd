"""Pipeline — discover → scrape → enrich → publish, end-to-end.

The legacy orchestrator lives at the repo root in `ai.py`. This module will
expose a fluent `Pipeline().discover().scrape().enrich().publish()` API once
the scrapers are fully ported.
"""
