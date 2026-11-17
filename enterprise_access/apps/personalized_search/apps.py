"""App config for Personalized Search (Chat)."""
from django.apps import AppConfig


class PersonalizedSearchConfig(AppConfig):
    """
    Personalized Search: a streaming chat turn that composes a learning page from the
    enterprise catalog (tech spec "Personalized Search (Chat)").
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'enterprise_access.apps.personalized_search'
