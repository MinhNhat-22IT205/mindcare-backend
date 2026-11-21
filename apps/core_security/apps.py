from django.apps import AppConfig


class CoreSecurityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core_security"
    verbose_name = "Core Security (AES-256-GCM)"
