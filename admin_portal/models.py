from django.conf import settings
from django.db import models


class PlatformSetting(models.Model):
    """
    Stores non-sensitive, operational settings for the MedAccess healthcare platform.
    Never stores credentials, secret keys, or passwords.
    """
    platform_name = models.CharField(max_length=150, default="MedAccess Healthcare Platform")
    support_email = models.EmailField(default="support@medaccess.com")
    support_phone = models.CharField(max_length=20, default="+91 1800-123-4567")
    emergency_contact = models.CharField(max_length=20, default="+91 1800-999-0000")
    operating_hours = models.CharField(max_length=100, default="24/7 Platform Assistance")
    maintenance_mode = models.BooleanField(default=False)
    allow_pharmacy_registration = models.BooleanField(default=True)
    allow_patient_registration = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='platform_settings_updates'
    )

    class Meta:
        verbose_name = "Platform Setting"
        verbose_name_plural = "Platform Settings"

    def __str__(self):
        return f"{self.platform_name} Settings (Updated: {self.updated_at.strftime('%Y-%m-%d %H:%M') if self.updated_at else 'Initial'})"

    @classmethod
    def get_settings(cls):
        """
        Singleton retrieval pattern: retrieves or creates the single configuration record.
        """
        obj, _ = cls.objects.get_or_create(id=1)
        return obj
