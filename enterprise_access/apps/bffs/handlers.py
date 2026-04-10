"""
Handlers for bffs app.

This module re-exports all handler classes for backwards compatibility.
New code can import directly from the specific modules:
- BaseHandler: enterprise_access.apps.bffs.base
- Learner portal handlers: enterprise_access.apps.bffs.learner_portal.handlers
"""
from enterprise_access.apps.bffs.base import BaseHandler
from enterprise_access.apps.bffs.learner_portal.handlers import (
    AcademyHandler,
    BaseLearnerPortalHandler,
    DashboardHandler,
    SearchHandler,
    SkillsQuizHandler
)

__all__ = [
    'BaseHandler',
    'BaseLearnerPortalHandler',
    'DashboardHandler',
    'SearchHandler',
    'AcademyHandler',
    'SkillsQuizHandler',
]
