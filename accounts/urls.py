from django.urls import path
from . import views

app_name = 'accounts'

urlpatterns = [
    path('login/', views.login_view, name='login'),
    path('register/patient/', views.register_patient_view, name='register_patient'),
    path('register/pharmacy/', views.register_pharmacy_view, name='register_pharmacy'),
    path('logout/', views.logout_view, name='logout'),
    path('verify-email/<str:token>/', views.verify_email_view, name='verify_email'),
    path('verify-email/', views.verify_email_view, name='verify_email_query'),
    path('resend-verification/', views.resend_verification_view, name='resend_verification'),
    path('password-reset/', views.MedAccessPasswordResetView.as_view(), name='password_reset'),
    path('password-reset/done/', views.MedAccessPasswordResetDoneView.as_view(), name='password_reset_done'),
    path('password-reset-confirm/<uidb64>/<token>/', views.MedAccessPasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('password-reset-complete/', views.MedAccessPasswordResetCompleteView.as_view(), name='password_reset_complete'),
]

