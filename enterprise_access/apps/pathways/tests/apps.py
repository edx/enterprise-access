""" App config for pathways test models, required for unit testing. """

from django.apps import AppConfig


class PathwaysTestsConfig(AppConfig):
    """
    App config for concrete implementations of the pathways abstract models.

    Needs an explicit ``label``: ``enterprise_access.apps.workflow.tests`` is also an
    installed app under test, and both would otherwise default to the label ``tests``.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'enterprise_access.apps.pathways.tests'
    label = 'pathways_tests'
