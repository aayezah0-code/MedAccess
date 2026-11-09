from django.contrib.auth.models import AbstractUser, UserManager as BaseUserManager
from django.db import models


class CustomUserManager(BaseUserManager):
    """
    Custom manager that assigns role='ADMIN' by default when createsuperuser is executed.
    """
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault('role', 'ADMIN')
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('email_verified', True)

        if extra_fields.get('role') != 'ADMIN':
            raise ValueError('Superuser must have role="ADMIN".')

        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    ROLE_CHOICES = (
        ('PATIENT', 'Patient'),
        ('PHARMACY', 'Pharmacy'),
        ('ADMIN', 'Admin'),
    )

    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='PATIENT')
    phone_number = models.CharField(max_length=15, blank=True, null=True)
    is_approved = models.BooleanField(default=True)
    email_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = CustomUserManager()

    def __str__(self):
        return f"{self.username} ({self.role})"
