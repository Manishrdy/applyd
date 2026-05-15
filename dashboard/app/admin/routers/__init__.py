"""Admin router registration.

`register_admin(app)` is the single integration point for `app.main` — it
mounts every admin router under their owning prefix. Adding a new admin
area means: write a new router module here, then add one line below.
"""

from fastapi import FastAPI

from app.admin.routers import (
    audit as audit_router,
    backups as backups_router,
    catchall as catchall_router,
    failed_logins as failed_logins_router,
    maintenance as maintenance_router,
    pages as pages_router,
    rate_limits as rate_limits_router,
    sessions as sessions_router,
    system as system_router,
)


def register_admin(app: FastAPI) -> None:
    # JSON APIs first — keep the catch-all dead last so it doesn't shadow.
    app.include_router(sessions_router.router, prefix="/api/admin", tags=["admin"])
    app.include_router(failed_logins_router.router, prefix="/api/admin", tags=["admin"])
    app.include_router(rate_limits_router.router, prefix="/api/admin", tags=["admin"])
    app.include_router(maintenance_router.router, prefix="/api/admin", tags=["admin"])
    app.include_router(backups_router.router, prefix="/api/admin", tags=["admin"])
    app.include_router(audit_router.router, prefix="/api/admin", tags=["admin"])
    app.include_router(system_router.router, prefix="/api/admin", tags=["admin"])
    app.include_router(pages_router.router, tags=["admin-pages"])
    # MUST be last. FastAPI matches routes in registration order; the
    # catchall would otherwise eat every more-specific admin path.
    app.include_router(catchall_router.router, tags=["admin-pages"])
