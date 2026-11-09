"""
URL configuration for MedAccess project.
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from accounts.views import landing_view
from patient import allergy_checker_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', landing_view, name='landing'),
    path('allergy-checker/', allergy_checker_views.allergy_checker_view, name='allergy_checker'),
    path('allergy-checker/api/', allergy_checker_views.allergy_checker_api, name='allergy_checker_api'),
    path('accounts/', include('accounts.urls', namespace='accounts')),
    path('patient/', include('patient.urls', namespace='patient')),
    path('pharmacy/', include('pharmacy.urls', namespace='pharmacy')),
    path('admin-portal/', include('admin_portal.urls', namespace='admin_portal')),
]

# Serve user-uploaded media files in development
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
