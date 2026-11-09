from django.urls import path
from . import views
from . import medicine_checker_views
from . import messaging_views
from . import notification_views
from . import allergy_checker_views

app_name = 'patient'

urlpatterns = [
    path('allergy-checker/', allergy_checker_views.allergy_checker_view, name='allergy_checker'),
    path('allergy-checker/api/', allergy_checker_views.allergy_checker_api, name='allergy_checker_api'),
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('profile/', views.profile_view, name='profile'),
    path('profile/update-location/', views.update_location_view, name='update_location'),
    path('prescriptions/', views.prescription_list_view, name='prescriptions_list'),
    path('prescriptions/upload/', views.upload_prescription_view, name='upload_prescription'),
    path('prescriptions/<int:pk>/', views.prescription_detail_view, name='prescription_detail'),
    path('prescriptions/<int:pk>/confirm-medicine/', views.confirm_prescription_medicine_view, name='confirm_prescription_medicine'),
    path('prescriptions/<int:pk>/confirm-extracted/', views.confirm_extracted_medicine_view, name='confirm_extracted_medicine'),
    path('prescriptions/<int:pk>/medicines/<int:med_id>/delete/', views.delete_prescription_medicine_view, name='delete_prescription_medicine'),
    path('prescriptions/<int:pk>/check-pharmacies/', views.check_pharmacies_availability_view, name='check_pharmacies_availability'),
    path('prescriptions/<int:pk>/send-request/', views.send_prescription_request_view, name='send_prescription_request'),
    path('prescriptions/<int:pk>/image/', views.prescription_secure_image_view, name='prescription_secure_image'),
    path('requests/', views.request_list_view, name='requests_list'),
    path('requests/all/', views.request_list_view, name='request_list'),
    path('requests/<int:pk>/', views.request_detail_view, name='request_detail'),
    path('requests/<int:pk>/cancel/', views.cancel_prescription_request_view, name='cancel_prescription_request'),
    path('requests/<int:pk>/messages/', messaging_views.request_messages_api, name='request_messages'),
    path('notifications/api/', notification_views.notifications_list_api, name='notifications_api'),
    path('notifications/unread-count/', notification_views.notification_unread_count_api, name='notification_unread_count'),
    path('notifications/<int:pk>/read/', notification_views.notification_mark_read_api, name='notification_mark_read'),
    path('notifications/mark-all-read/', notification_views.notifications_mark_all_read_api, name='notifications_mark_all_read'),
    path('nearby-pharmacies/', views.nearby_pharmacies_view, name='nearby_pharmacies'),
    path('nearby-pharmacies/api/', views.nearby_pharmacies_api, name='nearby_pharmacies_api'),
    path('saved-pharmacies/', views.saved_pharmacies_list_view, name='saved_pharmacies'),
    path('saved-pharmacies/save/', views.save_pharmacy_view, name='save_pharmacy'),
    path('saved-pharmacies/unsave/', views.unsave_pharmacy_view, name='unsave_pharmacy'),
    path('saved-pharmacies/<int:pk>/remove/', views.remove_saved_pharmacy_view, name='remove_saved_pharmacy'),
    path('feedback/', views.feedback_view, name='feedback'),
    path('medicine-checker/', medicine_checker_views.medicine_checker_view, name='medicine_checker'),
    path('medicine-checker/api/', medicine_checker_views.medicine_checker_api, name='medicine_checker_api'),
    path('medicine-checker/autocomplete/api/', medicine_checker_views.medicine_checker_autocomplete_api, name='medicine_checker_autocomplete_api'),
]
