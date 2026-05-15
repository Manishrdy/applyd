"""FastAPI dependencies for the admin module.

These are the only auth/authorization primitives admin routes should depend
on. By centralising them here we keep the routers ignorant of *how* a user
is authenticated (cookie + identity-service verify) and *how* their role is
resolved. If we later swap the identity backend for JWT, only this file
changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status


@dataclass(frozen=True)
class AdminUser:
    """Minimal view of the signed-in admin, populated by `auth_middleware`."""

    id: int
    email: str
    role: str


def get_current_user(request: Request) -> AdminUser:
    """Return the authenticated user populated by `auth_middleware`.

    Raises 401 if the middleware did not attach a user — this happens when
    the route is reached without a valid session (shouldn't be possible
    through the middleware, but the dependency stays defensive).
    """
    user_id = getattr(request.state, "user_id", None)
    if not isinstance(user_id, int):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")
    return AdminUser(
        id=user_id,
        email=getattr(request.state, "user_email", "") or "",
        role=getattr(request.state, "user_role", "user") or "user",
    )


def require_admin_user(request: Request) -> AdminUser:
    """Allow only admins through. Returns the admin user for use in handlers.

    Use as `admin: AdminUser = Depends(require_admin_user)`.
    """
    user = get_current_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    return user
