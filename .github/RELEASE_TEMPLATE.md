## Version
`vX.Y.Z` - YYYY-MM-DD

## Highlights
- 
- 
- 

## Added
- 

## Changed
- 

## Fixed
- 

## Removed
- 

## Upgrade Notes
- Database / schema changes:
- New or changed env vars:
- One-time commands to run:
  ```bash
  cd dashboard
  uv sync
  uv run python -m app.cli ingest
  ```

## Dashboard Impact
- Search/filter behavior:
- Saved jobs workflow:
- Scrape operations (`/scrape`) changes:
- Stats/analytics changes:

## Data & Freshness
- ATS sources added/removed/updated:
- Retention/freshness logic changes:
- Backfill/reingest required: Yes/No

## Known Issues
- None

## Artifacts
- Source code only
- Optional binaries/packages:

## Verification
- [ ] Tests pass locally (`cd dashboard && uv run --group dev pytest`)
- [ ] Manual smoke test completed (`/`, `/saved`, `/scrape`, `/stats`, `/settings`)

## Links
- Compare: `https://github.com/<org>/<repo>/compare/vPREV...vX.Y.Z`
- PRs included:
- Issues resolved:
