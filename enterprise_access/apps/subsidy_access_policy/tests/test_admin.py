"""
Tests for the admin module.
"""
import json
from datetime import date, datetime, timedelta
from unittest import mock
from uuid import uuid4

import ddt
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import resolve, reverse
from django.utils import timezone

from ...core.tests.factories import UserFactory
from ..admin import ForcedPolicyRedemptionAdmin, ForcedPolicyRedemptionForm
from ..admin.forms import BulkForcedPolicyRedemptionForm
from ..admin.utils import UrlNames
from ..api import GEAG_DATETIME_FMT
from ..models import ForcedPolicyRedemption
from .factories import ForcedPolicyRedemptionFactory, PerLearnerSpendCapLearnerCreditAccessPolicyFactory


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
            lms_user_id=123,
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

    def test_force_redeem_failure_is_recorded(self):
        forced_redemption_admin = ForcedPolicyRedemptionAdmin(
            model=ForcedPolicyRedemption,
            admin_site=AdminSite(),
        )
        forced_redemption = ForcedPolicyRedemptionFactory(
            subsidy_access_policy=PerLearnerSpendCapLearnerCreditAccessPolicyFactory(),
            lms_user_id=123,
            content_price_cents=100,
        )
        # No User record for 123, so building the GEAG metadata fails before force_redeem() is called.
        form = ForcedPolicyRedemptionForm(data={
            'geag_first_name': 'Foo',
            'geag_last_name': 'Bar',
            'geag_date_of_birth': date(2000, 1, 1),
            'lms_user_id': 123,
        })

        forced_redemption_admin.save_model(
            obj=forced_redemption,
            request=mock.Mock(user=UserFactory(lms_user_id=98765)),  # real user for the history record
            form=form,
            change=None,
        )

        forced_redemption.refresh_from_db()
        self.assertIsNotNone(forced_redemption.errored_at)
        self.assertIn('DoesNotExist', forced_redemption.traceback)


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
            ForcedPolicyRedemption,
            'force_redeem',
            autospec=True,
            side_effect=fake_force_redeem,
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
        (json.dumps([{'lms_user_id': 1, 'wait_to_redeem': True}]), 'Row 1: unknown keys'),
        (json.dumps([{'lms_user_id': 1}]), 'Row 1: subsidy_access_policy'),
        (json.dumps([{'lms_user_id': 1, 'geag_first_name': 'Ada'}]), 'Provide all or none of geag_first_name'),
        (json.dumps([{'lms_user_id': 1, 'geag_date_of_birth': '10/12/1990'}]), 'Row 1: geag_date_of_birth'),
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

    def _post_and_run_chain(self, rows):
        # CELERY_TASK_ALWAYS_EAGER runs the queued chain inline.
        return self._post(rows, follow=True)

    def test_batch_is_listed_in_queue_order(self):
        batch_uuid = uuid4()
        records = [
            ForcedPolicyRedemptionFactory(
                subsidy_access_policy=self.policy,
                batch_uuid=batch_uuid,
                content_price_cents=1,
            )
            for _ in range(3)
        ]
        # Make creation order the reverse of insertion order, so the assertion can't pass by accident.
        queue_order = list(reversed(records))
        for minutes, record in enumerate(queue_order):
            ForcedPolicyRedemption.objects.filter(uuid=record.uuid).update(
                created=timezone.now() + timedelta(minutes=minutes),
            )

        response = self.client.get(self.changelist_url, {'batch_uuid': str(batch_uuid)})

        self.assertEqual(list(response.context['cl'].result_list), queue_order)

    def test_view_is_not_atomic_request(self):
        # The view must commit the records itself before queueing tasks that read them.
        self.assertTrue(getattr(resolve(self.url).func, '_non_atomic_requests', None))

    def test_queued_task_kwargs_are_json_serializable(self):
        rows = [
            self._row(
                lms_user_id=123,
                geag_first_name='Ada',
                geag_last_name='Lovelace',
                geag_date_of_birth='1990-12-10',
                fallback_external_fulfillment_reference_id=str(uuid4()),
            ),
        ]

        with mock.patch('enterprise_access.apps.subsidy_access_policy.admin.chain') as mock_chain:
            self._post(rows)

        # Eager mode never serializes task args, so check what prod's JSON serializer would get.
        (signature,) = mock_chain.call_args.args
        self.assertEqual(json.loads(json.dumps(signature.kwargs)), signature.kwargs)
        self.assertEqual(signature.kwargs['geag_metadata']['geag_date_of_birth'], '1990-12-10')
        mock_chain.return_value.apply_async.assert_called_once_with()

    def test_queueing_failure_is_reported(self):
        with mock.patch('enterprise_access.apps.subsidy_access_policy.admin.chain') as mock_chain:
            mock_chain.return_value.apply_async.side_effect = Exception('broker down')
            with self.assertLogs('enterprise_access.apps.subsidy_access_policy', level='ERROR'):
                response = self._post([self._row()], follow=True)

        record = ForcedPolicyRedemption.objects.get()
        self.assertRedirects(response, f'{self.changelist_url}?batch_uuid={record.batch_uuid}')
        self.assertContains(response, f'Saved batch {record.batch_uuid}, but could not queue it for redemption.')
        self.mock_force_redeem.assert_not_called()

    def test_creates_batch_and_redeems_each_row(self):
        UserFactory(lms_user_id=123, email='ada@example.com')
        rows = [
            self._row(
                lms_user_id=123,
                geag_first_name='Ada',
                geag_last_name='Lovelace',
                geag_date_of_birth='1990-12-10',
            ),
            self._row(lms_user_id=456, suppress_assignment_email=True),
        ]

        response = self._post_and_run_chain(rows)

        records = {record.lms_user_id: record for record in ForcedPolicyRedemption.objects.all()}
        batch_uuid = records[123].batch_uuid
        self.assertIsNotNone(batch_uuid)
        self.assertEqual({record.batch_uuid for record in records.values()}, {batch_uuid})
        self.assertRedirects(response, f'{self.changelist_url}?batch_uuid={batch_uuid}')
        self.assertContains(response, f'Queued 2 forced redemption(s) as batch {batch_uuid}')
        self.assertContains(response, f'href="{self.changelist_url}?batch_uuid={batch_uuid}"')
        self.assertTrue(records[123].transaction_uuid and records[456].transaction_uuid)
        self.assertEqual(LogEntry.objects.filter(user=self.admin_user, action_flag=ADDITION).count(), 2)
        self.assertEqual(self.mock_force_redeem.call_args_list, [
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
                records[456],
                extra_metadata=None,
                actor_lms_user_id=98765,
                suppress_assignment_email=True,
            ),
        ])

    def test_failed_row_does_not_stop_the_chain(self):
        # No User record for 456, so building its GEAG metadata fails.
        rows = [
            self._row(lms_user_id=123),
            self._row(
                lms_user_id=456,
                geag_first_name='Ada',
                geag_last_name='Lovelace',
                geag_date_of_birth='1990-12-10',
            ),
            self._row(lms_user_id=789),
        ]

        with self.assertLogs('enterprise_access.apps.subsidy_access_policy', level='INFO') as logs:
            self._post_and_run_chain(rows)

        records = {record.lms_user_id: record for record in ForcedPolicyRedemption.objects.all()}
        self.assertIsNotNone(records[123].transaction_uuid)
        self.assertIsNotNone(records[789].transaction_uuid)
        failed = records[456]
        self.assertIsNone(failed.transaction_uuid)
        self.assertIsNotNone(failed.errored_at)
        self.assertIn('DoesNotExist', failed.traceback)
        bulk_logs = [line for line in logs.output if '[BULK FORCED REDEMPTION]' in line]
        # Eager mode runs the chain before the view logs that it queued it.
        self.assertIn(f'batch={failed.batch_uuid} queued 3 rows by lms_user_id=98765', bulk_logs[-1])
        self.assertIn(
            f'ERROR:enterprise_access.apps.subsidy_access_policy.tasks:[BULK FORCED REDEMPTION] '
            f'batch={failed.batch_uuid} NOT_REDEEMED forced_redemption={failed.uuid} policy={self.policy.uuid} '
            f'lms_user_id=456 course_run_key={self.course_run_key} content_price_cents=385000',
            logs.output,
        )
        self.assertIn(
            f'INFO:enterprise_access.apps.subsidy_access_policy.tasks:[BULK FORCED REDEMPTION] '
            f'batch={failed.batch_uuid} REDEEMED forced_redemption={records[789].uuid} policy={self.policy.uuid} '
            f'lms_user_id=789 course_run_key={self.course_run_key} content_price_cents=385000 '
            f'transaction_uuid={records[789].transaction_uuid}',
            logs.output,
        )
