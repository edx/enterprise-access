"""
Base handler for bffs app.
"""
import logging

from enterprise_access.apps.bffs.context import BaseHandlerContext

logger = logging.getLogger(__name__)


class BaseHandler:
    """
    A base handler class that provides shared core functionality for different BFF handlers.
    The `BaseHandler` includes core methods for loading data and adding errors to the context.
    """

    def __init__(self, context: BaseHandlerContext):
        """
        Initializes the BaseHandler with a HandlerContext.
        Args:
            context (HandlerContext): The context object containing request information and data.
        """
        self.context = context

    def load_and_process(self):
        """
        Loads and processes data. This method should be extended by subclasses to implement
        specific data loading and transformation logic.
        """
        raise NotImplementedError("Subclasses must implement `load_and_process` method.")

    def add_error(self, user_message, developer_message, status_code=None):
        """
        Adds an error to the context.
        Output fields determined by the ErrorSerializer
        """
        self.context.add_error(
            user_message=user_message,
            developer_message=developer_message,
            status_code=status_code,
        )

    def add_warning(self, user_message, developer_message):
        """
        Adds an error to the context.
        Output fields determined by the WarningSerializer
        """
        self.context.add_warning(
            user_message=user_message,
            developer_message=developer_message,
        )
