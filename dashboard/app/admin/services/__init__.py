"""Service layer for the admin module.

Routers (HTTP) call services (business logic). Services never import from
routers, never read `Request`, and never render templates. They take plain
data in, return plain data out, raise domain exceptions on bad inputs.
"""
