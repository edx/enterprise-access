"""
Tasks for subsidy requests app.
"""

import logging
from datetime import datetime

from celery import shared_task
from django.apps import apps
from django.conf import settings
from requests.exceptions import HTTPError

from enterprise_access.apps.api_client.braze_client import BrazeApiClient
from enterprise_access.apps.api_client.discovery_client import DiscoveryApiClient
from enterprise_access.apps.api_client.lms_client import LmsApiClient
from enterprise_access.apps.subsidy_request.constants import SubsidyRequestStates
from enterprise_access.tasks import LoggedTaskWithRetry
from enterprise_access.utils import get_subsidy_model

logger = logging.getLogger(__name__)

@shared_task(base=LoggedTaskWithRetry)
def psg_send_admins_email_with_new_requests_task(enterprise_customer_uuid):
    """
    Task to send new-request emails to admins.

    Args:
        enterprise_customer_uuid (str): enterprise customer uuid identifier
    Raises:
        HTTPError if Braze client callfails with an HTTPError
    """
    lms_client = LmsApiClient()
    enterprise_customer_data = lms_client.get_enterprise_customer_data(enterprise_customer_uuid)

    config_model = apps.get_model('subsidy_request.SubsidyRequestCustomerConfiguration')
    customer_config = config_model.objects.get(
        enterprise_customer_uuid=enterprise_customer_uuid,
    )

    subsidy_model = get_subsidy_model(customer_config.subsidy_type)
    subsidy_requests = subsidy_model.objects.filter(
        enterprise_customer_uuid=enterprise_customer_uuid,
        state=SubsidyRequestStates.REQUESTED,
    )
    # Filter when we last run this unless we never ran before
    # "future" is greater than "past"
    # so if created is greater than last remind date, it means
    # it was created after cron was last run
    if customer_config.last_remind_date is not None:
        subsidy_requests = subsidy_requests.filter(created__gte=customer_config.last_remind_date)

    subsidy_requests = subsidy_requests.order_by("-created")

    if not subsidy_requests:
        logger.info(
            'No new subsidy requests. Not sending new requests '
            f'email to admins for enterprise {enterprise_customer_uuid}.'
            )
        return

    braze_trigger_properties = {}
    enterprise_slug = enterprise_customer_data['slug']

    if subsidy_model == apps.get_model('subsidy_request.LicenseRequest'):
        subsidy_string = 'subscriptions'
    else:
        subsidy_string = 'coupons'

    url = f'{settings.ENTERPRISE_ADMIN_PORTAL_URL}/{enterprise_slug}/admin/{subsidy_string}/manage-requests'
    braze_trigger_properties['manage_requests_url'] = url

    braze_trigger_properties['requests'] = []
    for subsidy_request in subsidy_requests:

        user_email = subsidy_request.user.email
        course_title = subsidy_request.course_title

        braze_trigger_properties['requests'].append({
            'user_email': user_email,
            'course_title': course_title,
        })

    admin_users = enterprise_customer_data['admin_users']

    logger.info(
        f'Sending new-requests email to admins for enterprise {enterprise_customer_uuid}. '
        f'The email includes {len(subsidy_requests)} subsidy requests.'
    )
    braze_client = BrazeApiClient()
    recipients = [
        braze_client.create_recipient(
         user_email=admin_user['email'],
         lms_user_id=admin_user['lms_user_id']
        )
        for admin_user in admin_users
    ]
    try:
        braze_client.send_campaign_message(
            settings.BRAZE_NEW_REQUESTS_NOTIFICATION_CAMPAIGN,
            recipients=recipients,
            trigger_properties=braze_trigger_properties,
        )
    except HTTPError as exc:
        logger.exception(exc)
        raise

    customer_config.last_remind_date = datetime.now()
    customer_config.save()