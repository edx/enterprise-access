"""
Forms to be used for subsidy_access_policy django admin.
"""
import json

import requests
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from enterprise_access.apps.subsidy_access_policy.api import GeagMetadata
from enterprise_access.apps.subsidy_access_policy.utils import get_versioned_subsidy_client

from ..models import ForcedPolicyRedemption, SubsidyAccessPolicy


class LateRedemptionDaysFromNowChoices:
    """
    Enumerate different choices for the type of Subsidy.  For example, this can be used to control whether enrollments
    associated with this Subsidy should be rev rec'd through our standard commercial process or not.
    """
    DISABLE_NOW = "disable_now"
    CHOICES = (
        (DISABLE_NOW, _("Disable now")),
        ("1", _("1")),
        ("2", _("2")),
        ("3", _("3")),
        ("4", _("4")),
        ("5", _("5")),
        ("6", _("6")),
        ("7", _("7")),
    )


class SetLateRedemptionForm(forms.Form):
    """
    Form to set late redemption timeline.
    """
    days_from_now = forms.ChoiceField(
        label=_("Enable late redemptions until _ days from now"),
        choices=LateRedemptionDaysFromNowChoices.CHOICES,
        help_text=_("Unless disabled now, late redemptions will be disabled at midnight UTC of the selected day."),
        required=True,
    )


class DepositFundsForm(forms.Form):
    """
    Form to deposit funds into a subsidy from a policy.
    """
    desired_quantity_usd = forms.FloatField(
        label=_("Top-up quantity in USD"),
        help_text=_(
            "Amount of funds to add to the associated Subsidy, and also by which to increase this Policy's spend_limit."
        ),
        required=True,
        min_value=1,
    )
    sales_contract_reference_id = forms.RegexField(
        label=settings.SALES_CONTRACT_REFERENCE_PROVIDER_NAME,
        help_text=_(
            "Reference the original sales object that originated this deposit. "
            "This must be the 18-character case-insensitive version of the ID."
        ),
        required=True,
        regex="^00[kK][0-9a-zA-Z]{15}$",
        error_messages={
            "invalid": 'Salesforce Opportunity Line Item ID is invalid. Must be 18 characters and start with "00k".',
            "required": 'Salesforce Opportunity Line Item ID is required.',
        }
    )


class ForcedPolicyRedemptionForm(forms.ModelForm):
    """
    Admin form for the ForcedPolicyRedemption model.
    """
    geag_first_name = forms.CharField(
        label=_("Learner First Name (EE-only)"),
        help_text=_(
            "First name of learner, only used for Exec Ed Redemptions. "
            "This value (along with last name and D.O.B.) are not persisted; "
            "if you select 'Wait to redeem', the values will be gone when you try to save later."
        ),
        required=False,
    )
    geag_last_name = forms.CharField(
        label=_("Learner Last Name (EE-only)"),
        help_text=_("Last name of learner, only used for Exec Ed Redemptions"),
        required=False,
    )
    geag_date_of_birth = forms.DateField(
        label=_("Learner Date of Birth (EE-only)"),
        help_text=_("Learner date of birth (YYYY-MM-DD), only used for Exec Ed Redemptions"),
        required=False,
        input_formats=['%Y-%m-%d'],  # ISO only, so DD/MM dates can't be silently read as MM/DD
    )
    fallback_external_fulfillment_reference_id = forms.UUIDField(
        label=_("Order UUID of existing GEAG allocation (EE-only)"),
        help_text=_(
            "Link new transaction to a specific Order UUID. This is rarely needed, "
            "as a GEAG allocation will typically not yet exist and neither will an "
            "Order UUID. Only used for Exec Ed Redemptions."
        ),
        required=False,
    )
    suppress_assignment_email = forms.BooleanField(
        label=_("Suppress 'new assignment' email"),
        help_text=_(
            "For assignment-based budgets, don't email the learner when their assignment is created or "
            "re-allocated by this redemption. Like the fields above, this value is not persisted."
        ),
        required=False,
    )

    class Meta:
        model = ForcedPolicyRedemption
        fields = '__all__'

    def clean(self):
        """
        Requires all or none of the GEAG learner fields, which GEAG fulfillment needs together.
        """
        cleaned_data = super().clean()
        learner_fields = ('geag_first_name', 'geag_last_name', 'geag_date_of_birth')
        provided = [field for field in learner_fields if cleaned_data.get(field)]
        if provided and len(provided) != len(learner_fields):
            raise ValidationError(f'Provide all or none of {", ".join(learner_fields)}.')
        return cleaned_data

    def get_geag_metadata(self) -> GeagMetadata:
        """
        Returns this form's non-persisted GEAG fields for ``api.force_redeem_with_geag_metadata()``.
        Only valid after ``is_valid()``.
        """
        data = self.cleaned_data
        order_uuid = data.get('fallback_external_fulfillment_reference_id')
        return GeagMetadata(
            geag_first_name=data.get('geag_first_name') or '',
            geag_last_name=data.get('geag_last_name') or '',
            geag_date_of_birth=str(data.get('geag_date_of_birth') or ''),
            fallback_external_fulfillment_reference_id=str(order_uuid or ''),
        )


class SubsidyAccessPolicyForm(forms.ModelForm):
    """
    Admin form for the SubsidyAccessPolicy model.
    """
    def clean_subsidy_uuid(self):
        """
        Validate that the subsidy exists and is assigned to the same enterprise customer as the budget.
        """
        # 1. check if the subsidy_uuid actually exists
        # 2. subsidy is assigned to the same enterprise customer as the budget
        # if any of these checks fail, raise a ValidationError
        client = get_versioned_subsidy_client(version=1)
        try:
            subsidy = client.retrieve_subsidy(self.cleaned_data["subsidy_uuid"])
        except requests.exceptions.HTTPError as exc:
            raise ValidationError("Subsidy does not exist") from exc

        if str(subsidy["enterprise_customer_uuid"]) != str(self.cleaned_data["enterprise_customer_uuid"]):
            raise ValidationError("Subsidy is not assigned to the same enterprise customer as the budget")

        return self.cleaned_data["subsidy_uuid"]

    class Meta:
        model = SubsidyAccessPolicy
        fields = '__all__'


class BulkForcedPolicyRedemptionForm(forms.Form):
    """
    Admin form to create many ForcedPolicyRedemption records from a JSON list.

    Each object in the list holds the fields of the single-record ``ForcedPolicyRedemptionForm``
    and is validated by that form.
    """
    MAX_ROWS = 500

    redemptions_json = forms.CharField(
        label=_("Forced redemptions (JSON)"),
        widget=forms.Textarea(attrs={'rows': 20, 'cols': 120, 'style': 'font-family: monospace;'}),
        help_text=_("A JSON list with one object per forced redemption, at most {max_rows}.").format(
            max_rows=MAX_ROWS,
        ),
    )

    def clean_redemptions_json(self):
        """
        Parses the JSON, validates every row with ``ForcedPolicyRedemptionForm``, and returns the row forms.
        """
        try:
            rows = json.loads(self.cleaned_data['redemptions_json'])
        except json.JSONDecodeError as exc:
            raise ValidationError(f'Invalid JSON: {exc}') from exc
        if not isinstance(rows, list) or not rows:
            raise ValidationError('Expected a non-empty JSON list of objects.')
        if len(rows) > self.MAX_ROWS:
            raise ValidationError(f'Got {len(rows)} rows; at most {self.MAX_ROWS} are allowed per submission.')

        # redeemed_at/errored_at are read-only in the admin; every bulk row is redeemed, so no wait_to_redeem.
        allowed_keys = set(ForcedPolicyRedemptionForm().fields) - {'redeemed_at', 'errored_at', 'wait_to_redeem'}
        errors = []
        row_forms = []
        seen = {}
        for row_number, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                errors.append(f'Row {row_number}: expected an object.')
                continue
            unknown_keys = sorted(set(row) - allowed_keys)
            if unknown_keys:
                # Catches typos such as "lms_userid", which the form would otherwise silently ignore.
                errors.append(f'Row {row_number}: unknown keys {unknown_keys}.')
                continue
            row_form = ForcedPolicyRedemptionForm(data=row)
            if not row_form.is_valid():
                for field, field_errors in row_form.errors.items():
                    errors.append(f'Row {row_number}: {field}: {" ".join(field_errors)}')
                continue
            identity = (
                row_form.cleaned_data['subsidy_access_policy'].uuid,
                row_form.cleaned_data['lms_user_id'],
                row_form.cleaned_data['course_run_key'],
            )
            if identity in seen:
                errors.append(f'Row {row_number}: duplicates row {seen[identity]}.')
                continue
            seen[identity] = row_number
            row_forms.append(row_form)

        if errors:
            raise ValidationError(errors)
        return row_forms
