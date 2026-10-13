"""
Tests for the admin module.
"""
import json
from datetime import date, datetime
from unittest import mock
from uuid import uuid4

import ddt
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from ...core.tests.factories import UserFactory
from ..admin import GEAG_DATETIME_FMT, ForcedPolicyRedemptionAdmin, ForcedPolicyRedemptionForm
from ..admin.forms import BulkForcedPolicyRedemptionForm
from ..admin.utils import UrlNames
from ..models import ForcedPolicyRedemption
from .factories import PerLearnerSpendCapLearnerCreditAccessPolicyFactory


class ForcedPolicyRedemptionAdminTests(TestCase):
    """
    Tests for the ForcedPolicyRedemptionAdmin class.
    """
    def test_force_redeem_with_extra_metadata(self):
        forced_redemption_admin = ForcedPolicyRedemptionAdmin(
            model=ForcedPolicyRedemption,
            admin_site=AdminSite(),
        )
        request = mock.Mock()
        forced_redemption_obj = mock.Mock(
            transaction_uuid=None,
            wait_to_redeem=False,
        )
        UserFactory.create(lms_user_id=123, email='foo@bar.com')

        form = ForcedPolicyRedemptionForm(data={
            'geag_first_name': 'Foo',
            'geag_last_name': 'Bar',
            'geag_date_of_birth': date(2000, 1, 1),
            'lms_user_id': 123,
        })

        forced_redemption_admin.save_model(
            obj=forced_redemption_obj,
            request=request,
            form=form,
            change=None,
        )

        forced_redemption_obj.force_redeem.assert_called_once_with(
            extra_metadata={
                'geag_first_name': 'Foo',
                'geag_last_name': 'Bar',
                'geag_date_of_birth': '2000-01-01',
                'geag_terms_accepted_at': mock.ANY,
                'geag_data_share_consent': True,
                'geag_email': 'foo@bar.com',
            },
            actor_lms_user_id=request.user.lms_user_id,
            suppress_assignment_email=False,
        )
        terms_accepted_value =\
            forced_redemption_obj.force_redeem.call_args_list[0][1]['extra_metadata']['geag_terms_accepted_at']
        # we don't really care about the value, but
        # we want to know that parsing the value matches the datetime
        # format accepted by GEAG.
        assert datetime.strptime(terms_accepted_value, GEAG_DATETIME_FMT)


@ddt.ddt
class ForcedPolicyRedemptionBulkCreateTests(TestCase):
    """
    Tests for the "Bulk create from JSON" forced redemption admin view.
    """
    course_run_key = 'course-v1:edX+DemoX+1T2042'

    def setUp(self):
        super().setUp()
        self.url = reverse('admin:' + UrlNames.BULK_CREATE_FORCED_REDEMPTIONS)
        self.changelist_url = reverse('admin:subsidy_access_policy_forcedpolicyredemption_changelist')
        self.admin_user = UserFactory(is_staff=True, is_superuser=True, lms_user_id=98765)
        self.client.force_login(self.admin_user)
        self.policy = PerLearnerSpendCapLearnerCreditAccessPolicyFactory()

        # Stand-in for a successful redemption: force_redeem() sets transaction_uuid on the record.
        def fake_force_redeem(forced_redemption, **kwargs):
            forced_redemption.transaction_uuid = uuid4()
            forced_redemption.redeemed_at = timezone.now()
            forced_redemption.save()

        force_redeem_patcher = mock.patch.object(
            ForcedPolicyRedemption, 'force_redeem', autospec=True, side_effect=fake_force_redeem,
        )
        self.mock_force_redeem = force_redeem_patcher.start()
        self.addCleanup(force_redeem_patcher.stop)

    def _row(self, lms_user_id=123, **overrides):
        return {
            'subsidy_access_policy': str(self.policy.uuid),
            'lms_user_id': lms_user_id,
            'course_run_key': self.course_run_key,
            'content_price_cents': 385000,
            **overrides,
        }

    def _post(self, rows, **kwargs):
        payload = rows if isinstance(rows, str) else json.dumps(rows)
        return self.client.post(self.url, {'redemptions_json': payload}, **kwargs)

    def test_changelist_links_to_bulk_create(self):
        response = self.client.get(self.changelist_url)
        self.assertContains(response, 'Bulk create from JSON')

    def test_requires_add_permission(self):
        staff_user = UserFactory(is_staff=True)
        staff_user.user_permissions.add(Permission.objects.get(codename='view_forcedpolicyredemption'))
        self.client.force_login(staff_user)

        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self._post([self._row()]).status_code, 403)
        self.assertFalse(ForcedPolicyRedemption.objects.exists())

    @ddt.data(
        ('[{', 'Invalid JSON'),
        ('{}', 'non-empty JSON list'),
        ('[]', 'non-empty JSON list'),
        ('["x"]', 'Row 1: expected an object.'),
        (json.dumps([{'lms_userid': 1}]), 'Row 1: unknown keys'),
        (json.dumps([{'lms_user_id': 1, 'redeemed_at': '2024-01-01T00:00:00Z'}]), 'Row 1: unknown keys'),
        (json.dumps([{'lms_user_id': 1}]), 'Row 1: subsidy_access_policy'),
    )
    @ddt.unpack
    def test_invalid_payload_saves_nothing(self, payload, expected_error):
        response = self._post(payload)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, expected_error)
        self.assertFalse(ForcedPolicyRedemption.objects.exists())

    def test_one_invalid_row_saves_nothing(self):
        response = self._post([self._row(lms_user_id=1), self._row(lms_user_id=2, content_price_cents='x')])

        self.assertContains(response, 'Row 2: content_price_cents')
        self.assertFalse(ForcedPolicyRedemption.objects.exists())

    def test_duplicate_rows(self):
        response = self._post([self._row(), self._row()])

        self.assertContains(response, 'Row 2: duplicates row 1.')
        self.assertFalse(ForcedPolicyRedemption.objects.exists())

    def test_too_many_rows(self):
        with mock.patch.object(BulkForcedPolicyRedemptionForm, 'MAX_ROWS', 1):
            response = self._post([self._row(lms_user_id=1), self._row(lms_user_id=2)])

        self.assertContains(response, 'at most 1 are allowed')

    def test_creates_and_redeems_each_row(self):
        UserFactory(lms_user_id=123, email='ada@example.com')
        rows = [
            self._row(
                lms_user_id=123, geag_first_name='Ada', geag_last_name='Lovelace', geag_date_of_birth='1990-12-10',
            ),
            self._row(lms_user_id=456, suppress_assignment_email=True),
        ]

        response = self._post(rows, follow=True)

        self.assertRedirects(response, self.changelist_url)
        self.assertContains(response, '2 redeemed, 0 saved but not redeemed')
        records = {record.lms_user_id: record for record in ForcedPolicyRedemption.objects.all()}
        self.assertEqual(set(records), {123, 456})
        self.assertTrue(all(record.transaction_uuid for record in records.values()))
        self.assertEqual(
            LogEntry.objects.filter(user=self.admin_user, action_flag=ADDITION).count(), 2,
        )
        self.mock_force_redeem.assert_has_calls([
            mock.call(
                records[123],
                extra_metadata={
                    'geag_first_name': 'Ada',
                    'geag_last_name': 'Lovelace',
                    'geag_date_of_birth': '1990-12-10',
                    'geag_terms_accepted_at': mock.ANY,
                    'geag_data_share_consent': True,
                    'geag_email': 'ada@example.com',
                },
                actor_lms_user_id=98765,
                suppress_assignment_email=False,
            ),
            mock.call(
                records[456], extra_metadata=None, actor_lms_user_id=98765, suppress_assignment_email=True,
            ),
        ])

    def test_failed_row_is_reported_and_others_still_redeem(self):
        # No User record for 123, so building its GEAG metadata fails inside save_model().
        rows = [
            self._row(
                lms_user_id=123, geag_first_name='Ada', geag_last_name='Lovelace', geag_date_of_birth='1990-12-10',
            ),
            self._row(lms_user_id=456),
        ]

        with self.assertLogs('enterprise_access.apps.subsidy_access_policy.admin', level='INFO') as logs:
            response = self._post(rows, follow=True)

        self.assertContains(response, '1 redeemed, 1 saved but not redeemed')
        self.assertContains(response, 'Failure reason')
        failed = ForcedPolicyRedemption.objects.get(lms_user_id=123)
        succeeded = ForcedPolicyRedemption.objects.get(lms_user_id=456)
        bulk_logs = [line for line in logs.output if '[BULK FORCED REDEMPTION]' in line]
        self.assertEqual(len(bulk_logs), 3)
        self.assertTrue(bulk_logs[0].startswith('ERROR:'))
        self.assertIn(
            f'row=1/2 NOT_REDEEMED forced_redemption={failed.uuid} policy={self.policy.uuid} lms_user_id=123 '
            f'course_run_key={self.course_run_key} content_price_cents=385000 wait_to_redeem=False',
            bulk_logs[0],
        )
        self.assertTrue(bulk_logs[1].startswith('INFO:'))
        self.assertIn(
            f'row=2/2 REDEEMED forced_redemption={succeeded.uuid} policy={self.policy.uuid} lms_user_id=456 '
            f'course_run_key={self.course_run_key} content_price_cents=385000 '
            f'transaction_uuid={succeeded.transaction_uuid}',
            bulk_logs[1],
        )
        self.assertIn('finished by lms_user_id=98765: 2 rows, 1 redeemed, 1 not redeemed', bulk_logs[2])
        batch_ids = {line.split('batch=')[1].split()[0] for line in bulk_logs}
        self.assertEqual(len(batch_ids), 1)
