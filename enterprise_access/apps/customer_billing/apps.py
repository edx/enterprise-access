""" App config for customer_billing """

from django.apps import AppConfig


class CustomerBillingConfig(AppConfig):
    """ App config for customer_billing. """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'enterprise_access.apps.customer_billing'
    _signals_wired = False

    def ready(self):
        # Prevent duplicate receiver registration when Django reloads app configs.
        # Check and set on the class so the guard survives multiple AppConfig instantiations.
        if CustomerBillingConfig._signals_wired:
            return
        import enterprise_access.apps.customer_billing.signals  # pylint: disable=import-outside-toplevel,unused-import
        CustomerBillingConfig._signals_wired = True
