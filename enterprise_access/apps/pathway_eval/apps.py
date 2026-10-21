"""
App configuration for the learner pathway evaluation harness.
"""
from django.apps import AppConfig


class PathwayEvalConfig(AppConfig):
    """
    Evaluation harness for learner pathway recommendation quality.

    This app owns *no* pipeline logic. Retrieval, translation, re-ranking and
    assembly are production code elsewhere in the service; the harness only calls
    them, scores what comes back, and reports. If the harness reimplements any part
    of the pipeline, the harness is what gets measured.
    """
    name = 'enterprise_access.apps.pathway_eval'
    verbose_name = 'Learner Pathway Evaluation'
