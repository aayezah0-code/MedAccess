from django.contrib import admin
from .models import PharmacyProfile, Medicine, Inventory, Feedback, PharmacySupportRequest


@admin.register(PharmacyProfile)
class PharmacyProfileAdmin(admin.ModelAdmin):
    list_display = (
        'pharmacy_name',
        'owner_name',
        'phone_number',
        'city',
        'approval_status',
        'created_at'
    )
    list_filter = ('approval_status', 'city', 'state')
    search_fields = ('pharmacy_name', 'owner_name', 'pharmacy_license_number', 'gst_number')
    list_editable = ('approval_status',)


@admin.register(Medicine)
class MedicineAdmin(admin.ModelAdmin):
    list_display = ('medicine_name', 'manufacturer', 'dosage_form', 'mrp')
    search_fields = ('medicine_name', 'composition', 'manufacturer')


@admin.register(Inventory)
class InventoryAdmin(admin.ModelAdmin):
    list_display = ('pharmacy', 'medicine', 'batch_number', 'expiry_date', 'quantity', 'unit')
    list_filter = ('expiry_date', 'pharmacy')
    search_fields = ('medicine__medicine_name', 'batch_number')


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = ('pharmacy', 'patient', 'rating', 'created_at')
    list_filter = ('rating', 'created_at')


@admin.register(PharmacySupportRequest)
class PharmacySupportRequestAdmin(admin.ModelAdmin):
    list_display = ('subject', 'pharmacy', 'category', 'status', 'created_at')
    list_filter = ('category', 'status', 'created_at')
    search_fields = ('subject', 'message', 'pharmacy__pharmacy_name', 'submitted_by__username')
    readonly_fields = ('pharmacy', 'submitted_by', 'created_at', 'updated_at')
