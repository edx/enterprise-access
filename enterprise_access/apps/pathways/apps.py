"""
App configuration for the learner pathways pipeline.
"""
from django.apps import AppConfig


class PathwaysConfig(AppConfig):
    """
    Server-side learner pathway generation.

    Owns the workflows, steps and endpoints that turn a learner's intake into an
    ordered course pathway. Pipeline logic lives here, as production code; the
    evaluation harness in ``apps.pathway_eval`` only calls it and scores the result.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'enterprise_access.apps.pathways'
    verbose_name = 'Learner Pathways'
