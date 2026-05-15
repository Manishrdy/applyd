"""Admin subpackage — single-operator panel for the applyd dashboard.

Routers and services here are independently mountable so each concern
(sessions, backups, maintenance, etc.) stays in its own module. The package
re-exports `register_admin` to mount all admin routes from a single call.
"""

from app.admin.routers import register_admin

__all__ = ["register_admin"]
