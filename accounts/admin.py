from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'role', 'is_approved', 'is_staff', 'is_active')
    list_filter = ('role', 'is_approved', 'is_staff', 'is_active')
    fieldsets = UserAdmin.fieldsets + (
        ('MedAccess Role & Status', {'fields': ('role', 'phone_number', 'is_approved')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('MedAccess Role & Status', {'fields': ('role', 'phone_number', 'is_approved')}),
    )
