"""Tests for the enterprise-access feature toggles."""
import pytest
from edx_toggles.toggles import WaffleSwitch
from edx_toggles.toggles.testutils import override_waffle_switch

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
