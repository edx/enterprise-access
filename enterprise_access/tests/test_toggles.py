import enterprise_access.toggles as toggles


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
