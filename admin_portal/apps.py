from functools import update_wrapper
from django.apps import AppConfig
from django.core.exceptions import PermissionDenied


class AdminPortalConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'admin_portal'

    def ready(self):
        """
        Enforce strict predefined admin check on Django's built-in /admin/ site.
        Ensures only the two predefined identities can access Django administration.
        - Unauthenticated users: login flow is preserved (redirected to /admin/login/).
        - Authenticated unauthorized users (unlisted staff/superusers, patients, pharmacies):
          HTTP 403 Forbidden is returned via PermissionDenied.
        """
        from django.contrib.admin.sites import AdminSite
        from accounts.decorators import is_predefined_admin

        if not getattr(AdminSite, '_medaccess_secured', False):
            _orig_has_permission = AdminSite.has_permission
            _orig_admin_view = AdminSite.admin_view

            def secure_has_permission(self, request):
                if not _orig_has_permission(self, request):
                    return False
                return is_predefined_admin(request.user)

            def secure_admin_view(self, view, cacheable=False):
                view_func = _orig_admin_view(self, view, cacheable)

                def wrapped_view(request, *args, **kwargs):
                    if request.user.is_authenticated and not is_predefined_admin(request.user):
                        raise PermissionDenied("Access denied. You are not an authorized administrator.")
                    return view_func(request, *args, **kwargs)

                return update_wrapper(wrapped_view, view_func)

            AdminSite.has_permission = secure_has_permission
            AdminSite.admin_view = secure_admin_view
            AdminSite._medaccess_secured = True
