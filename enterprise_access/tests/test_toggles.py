"""Tests for the enterprise-access feature toggles."""
import pytest
from edx_toggles.toggles import WaffleFlag, WaffleSwitch
from edx_toggles.toggles.testutils import override_waffle_flag, override_waffle_switch

from enterprise_access import toggles


def test_enable_multi_license_entitlements_bff_enabled(monkeypatch):
    monkeypatch.setattr(
        toggles.ENABLE_MULTI_LICENSE_ENTITLEMENTS_BFF,
        'is_enabled',
        lambda: True
    )
    assert toggles.enable_multi_license_entitlements_bff() is True


def test_enable_multi_license_entitlements_bff_disabled(monkeypatch):
    monkeypatch.setattr(
        toggles.ENABLE_MULTI_LICENSE_ENTITLEMENTS_BFF,
        'is_enabled',
        lambda: False
    )
    assert toggles.enable_multi_license_entitlements_bff() is False


@pytest.mark.parametrize('switch', [
    toggles.LEARNER_PATHWAYS_SERVER_PIPELINE,
    toggles.LEARNER_PATHWAYS_DISABLE_CANDIDATE_RERANK,
])
def test_the_pathway_toggles_are_switches_not_flags(switch):
    """
    Both pathway toggles must stay ``WaffleSwitch``.

    This is a product requirement, not a style preference: a switch is a single global
    boolean an administrator sets in Django admin, and waffle honours ``?name=1``
    query-string overrides for *flags* only. Swapping either of these for a ``WaffleFlag``
    would make an unreleased, paid pipeline reachable by crafting a URL, and would also
    break the offline callers -- the harness and management commands have no request to
    evaluate a flag against.
    """
    assert isinstance(switch, WaffleSwitch)


@pytest.mark.django_db
def test_the_pipeline_is_disabled_by_default():
    """A switch that has never been created reads as off, which is the safe default."""
    assert toggles.learner_pathways_server_pipeline_enabled() is False


@pytest.mark.django_db
def test_the_pipeline_can_be_enabled():
    with override_waffle_switch(toggles.LEARNER_PATHWAYS_SERVER_PIPELINE, True):
        assert toggles.learner_pathways_server_pipeline_enabled() is True


@pytest.mark.django_db
def test_reranking_is_on_by_default_because_its_switch_is_a_kill_switch():
    """
    The re-rank toggle's polarity is inverted on purpose.

    An enable-style switch defaulting to off would mean enabling the pipeline yielded
    pathways with no model input at all -- courses in retrieval order, no error, a
    well-formed response. That silent degradation has already bitten this pipeline once,
    so it must not be reachable by forgetting a second switch.
    """
    assert toggles.learner_pathways_candidate_rerank_enabled() is True


@pytest.mark.django_db
def test_reranking_stops_when_the_kill_switch_is_on():
    with override_waffle_switch(toggles.LEARNER_PATHWAYS_DISABLE_CANDIDATE_RERANK, True):
        assert toggles.learner_pathways_candidate_rerank_enabled() is False


def test_personalized_search_is_a_per_user_flag():
    """Personalized Search rolls out per user, so it's a ``WaffleFlag`` (unlike the pathway switches)."""
    assert isinstance(toggles.PERSONALIZED_SEARCH_ENABLED, WaffleFlag)


def test_flags_cannot_be_overridden_from_the_url(settings):
    """
    ``?enterprise_access.personalized_search_enabled=1`` must not turn the endpoints on. django-waffle
    only honours such overrides when WAFFLE_OVERRIDE is set, so keep it unset.
    """
    assert not getattr(settings, 'WAFFLE_OVERRIDE', False)


@pytest.mark.django_db
def test_personalized_search_is_off_by_default_and_can_be_enabled():
    assert toggles.personalized_search_enabled() is False
    with override_waffle_flag(toggles.PERSONALIZED_SEARCH_ENABLED, active=True):
        assert toggles.personalized_search_enabled() is True


def test_ssp_paid_renewal_toggle_is_a_switch():
    """It is read in webhook handlers and Celery tasks, where there is no request to evaluate a flag against."""
    assert isinstance(toggles.SSP_PAID_RENEWAL, WaffleSwitch)


@pytest.mark.django_db
def test_ssp_paid_renewal_is_disabled_by_default():
    """A switch that has never been created reads as off, so existing behaviour runs until it is turned on."""
    assert toggles.ssp_paid_renewal_enabled() is False


@pytest.mark.django_db
def test_ssp_paid_renewal_can_be_enabled():
    with override_waffle_switch(toggles.SSP_PAID_RENEWAL, True):
        assert toggles.ssp_paid_renewal_enabled() is True


@pytest.mark.django_db
def test_personalized_search_suggested_order_is_off_by_default_and_can_be_enabled():
    """A switch, read by the turn view and the offline harness alike."""
    assert isinstance(toggles.PERSONALIZED_SEARCH_SUGGESTED_ORDER, WaffleSwitch)
    assert toggles.personalized_search_suggested_order_enabled() is False
    with override_waffle_switch(toggles.PERSONALIZED_SEARCH_SUGGESTED_ORDER, True):
        assert toggles.personalized_search_suggested_order_enabled() is True
