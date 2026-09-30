"""
App configuration for pathway editorial rules.
"""
from django.apps import AppConfig


class PathwayEditorialConfig(AppConfig):
    """
    Admin-editable editorial rules applied to learner pathway generation.

    Holds business decisions a product reviewer makes about which courses a pathway may
    or should carry, as data rather than code, so they change without a deploy and every
    edit is kept as a history row. The rules are read through ``api.load_policy`` and
    applied by the pure ``api.plan_seats``; this app does not call the pathway pipeline.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'enterprise_access.apps.pathway_editorial'
    label = 'pathway_editorial'
    verbose_name = 'Learner Pathway Editorial Rules'
