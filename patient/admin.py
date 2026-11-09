from django.contrib import admin
from .models import PatientProfile, Prescription, PrescriptionMedicine, SavedPharmacy, ChatHistory


@admin.register(PatientProfile)
class PatientProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'gender', 'blood_group', 'date_of_birth')
    search_fields = ('user__username', 'user__email', 'user__first_name', 'user__last_name')


@admin.register(Prescription)
class PrescriptionAdmin(admin.ModelAdmin):
    list_display = ('patient', 'status', 'uploaded_at')
    list_filter = ('status', 'uploaded_at')


@admin.register(PrescriptionMedicine)
class PrescriptionMedicineAdmin(admin.ModelAdmin):
    list_display = ('medicine_name', 'prescription', 'dosage', 'duration')


@admin.register(SavedPharmacy)
class SavedPharmacyAdmin(admin.ModelAdmin):
    list_display = ('patient', 'pharmacy', 'saved_at')


@admin.register(ChatHistory)
class ChatHistoryAdmin(admin.ModelAdmin):
    list_display = ('patient', 'created_at')
