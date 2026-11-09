from django.urls import path
from . import views
from patient.messaging_views import request_messages_api
from patient import notification_views

app_name = 'pharmacy'

urlpatterns = [
    path('', views.dashboard_view, name='dashboard'),
    path('requests/', views.requests_inbox_view, name='requests_inbox'),
    path('requests/<int:pk>/', views.request_detail_view, name='request_detail'),
    path('requests/<int:pk>/update-status/', views.request_update_status_view, name='request_update_status'),
    path('requests/<int:pk>/messages/', request_messages_api, name='request_messages'),
    path('notifications/api/', notification_views.notifications_list_api, name='notifications_api'),
    path('notifications/unread-count/', notification_views.notification_unread_count_api, name='notification_unread_count'),
    path('notifications/<int:pk>/read/', notification_views.notification_mark_read_api, name='notification_mark_read'),
    path('notifications/mark-all-read/', notification_views.notifications_mark_all_read_api, name='notifications_mark_all_read'),
    path('settings/', views.pharmacy_settings_view, name='settings'),
    path('resubmit/', views.pharmacy_resubmit_view, name='resubmit'),
    path('medicines/', views.medicines_list_view, name='medicines'),
    path('medicines/add/', views.medicine_add_view, name='medicine_add'),
    path('medicines/autocomplete/', views.medicine_autocomplete_api, name='medicine_autocomplete'),
    path('inventory/', views.inventory_list_view, name='inventory'),
    path('inventory/add/', views.inventory_add_view, name='inventory_add'),
    path('inventory/<int:pk>/update/', views.inventory_update_view, name='inventory_update'),
    path('update-stock/', views.smart_stock_entry_view, name='update_stock'),
    path('update-stock/parse-voice/', views.voice_stock_parse_api, name='voice_stock_parse'),
    path('update-stock/csv/analyze/', views.csv_stock_analyze_api, name='csv_stock_analyze'),
    path('update-stock/csv/validate/', views.csv_stock_validate_api, name='csv_stock_validate'),
    path('update-stock/csv/import/', views.csv_stock_import_api, name='csv_stock_import'),
    path('smart-stock-entry/', views.smart_stock_entry_view, name='smart_stock_entry'),
    path('customer-feedback/', views.customer_feedback_view, name='customer_feedback'),
    path('analytics/', views.analytics_view, name='analytics'),
    path('medicines/composition-lookup/api/', views.composition_lookup_api, name='composition_lookup_api'),
    path('medicines/composition-autocomplete/api/', views.composition_autocomplete_api, name='composition_autocomplete_api'),
    path('medicines/predict-price/api/', views.price_prediction_api, name='price_prediction_api'),
]
