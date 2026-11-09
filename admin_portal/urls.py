from django.urls import path
from . import views

app_name = 'admin_portal'

urlpatterns = [
    # Dashboard & Landing
    path('', views.dashboard_view, name='landing'),
    path('dashboard/', views.dashboard_view, name='dashboard'),

    # Patient Management
    path('patients/', views.patient_list_view, name='patient_list'),
    path('patients/add/', views.patient_add_view, name='patient_add'),
    path('patients/<int:pk>/', views.patient_detail_view, name='patient_detail'),
    path('patients/<int:pk>/edit/', views.patient_edit_view, name='patient_edit'),
    path('patients/<int:pk>/toggle-status/', views.patient_toggle_status_view, name='patient_toggle_status'),

    # Pharmacy Management & Verification
    path('pharmacies/', views.pharmacy_list_view, name='pharmacy_list'),
    path('pharmacies/<int:pk>/', views.pharmacy_detail_view, name='pharmacy_detail'),
    path('pharmacies/<int:pk>/approve/', views.pharmacy_approve_view, name='pharmacy_approve'),
    path('pharmacies/<int:pk>/reject/', views.pharmacy_reject_view, name='pharmacy_reject'),

    # Medicines Catalogue & Inventory Overview
    path('medicines/', views.medicine_inventory_view, name='medicine_list'),

    # Feedback and Issues
    path('feedback/', views.feedback_list_view, name='feedback_list'),

    # Platform Settings
    path('settings/', views.platform_settings_view, name='settings'),

    # Help & Support
    path('help/', views.help_support_view, name='help'),
]
