"""
DRF permission classes used by xblock-bunny's REST endpoints.

v0.1 keeps this deliberately coarse: only Django staff users can touch the
authoring / management endpoints. That's a superset of "course author" on
most Open edX deployments — instructors who need access can be granted the
``is_staff`` flag by a site admin. The webhook endpoint is token-authenticated
in its own handler (no session, no DRF perms).

A future v0.2 can layer in ``common.djangoapps.student.roles.CourseInstructorRole``
to allow per-course author access without ``is_staff``. Importing
``edx-platform`` modules from a generic XBlock package makes the package
brittle across releases, so it's intentionally avoided here.
"""

from rest_framework.permissions import BasePermission


class IsStaffUser(BasePermission):
    """Allow only authenticated users with ``is_staff`` (or superuser)."""

    message = "Staff-only endpoint."

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return bool(user.is_staff or user.is_superuser)
