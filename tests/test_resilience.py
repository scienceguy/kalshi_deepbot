"""Tests for resilience.py — cycle backoff and client-rebuild supervisor."""
from resilience import CycleSupervisor, ResilienceSettings
import config


class TestResilienceSettings:
    """Tests for ResilienceSettings defaults."""

    def test_defaults_match_config(self):
        s = ResilienceSettings()
        assert s.base_backoff == config.ERROR_BACKOFF_SECONDS
        assert s.max_backoff == config.ERROR_BACKOFF_MAX_SECONDS
        assert s.rebuild_threshold == config.CLIENT_REBUILD_THRESHOLD
        assert s.max_rebuilds == config.CLIENT_MAX_REBUILDS


class TestCycleSupervisor:
    """Tests for CycleSupervisor failure tracking and rebuild decisions."""

    def test_mark_success_resets_errors(self):
        s = CycleSupervisor()
        s.mark_failure(ValueError("boom"))
        assert s.errors_in_a_row == 1
        s.mark_success()
        assert s.errors_in_a_row == 0

    def test_mark_failure_increments(self):
        s = CycleSupervisor()
        s.mark_failure(ValueError("boom"))
        s.mark_failure(ValueError("boom"))
        assert s.errors_in_a_row == 2

    def test_should_rebuild_after_threshold(self):
        s = CycleSupervisor(settings=ResilienceSettings(rebuild_threshold=3, max_rebuilds=5))
        s.mark_failure(ValueError("boom"))
        s.mark_failure(ValueError("boom"))
        assert not s.should_rebuild_client()
        s.mark_failure(ValueError("boom"))
        assert s.should_rebuild_client()

    def test_should_rebuild_respects_max_rebuilds(self):
        s = CycleSupervisor(settings=ResilienceSettings(rebuild_threshold=1, max_rebuilds=0))
        s.mark_failure(ValueError("boom"))
        assert not s.should_rebuild_client()  # max_rebuilds = 0 → never rebuild

    def test_after_rebuild_resets_errors(self):
        s = CycleSupervisor(settings=ResilienceSettings(rebuild_threshold=1, max_rebuilds=5))
        s.mark_failure(ValueError("boom"))
        assert s.errors_in_a_row == 1
        s.after_rebuild()
        assert s.errors_in_a_row == 0
        assert s.rebuilds == 1

    def test_is_auth_error(self):
        s = CycleSupervisor()
        assert not s.is_auth_error()
        s.mark_failure(ValueError("Kalshi API error: 401 authentication failed"))
        assert s.is_auth_error()
        s.mark_failure(ValueError("403 permission denied"))
        assert s.is_auth_error()
        s.mark_failure(ValueError("network timeout"))
        assert not s.is_auth_error()

    def test_backoff_delay_grows(self):
        s = CycleSupervisor(settings=ResilienceSettings(base_backoff=5, max_backoff=60))
        # 1st failure → no backoff
        assert s.mark_failure(ValueError("boom")) == 0.0
        # 2nd failure → base_backoff
        assert s.mark_failure(ValueError("boom")) == 5.0
        # 3rd failure → 2× base
        assert s.mark_failure(ValueError("boom")) == 10.0
        # 4th failure → 4× base
        assert s.mark_failure(ValueError("boom")) == 20.0

    def test_backoff_delay_capped(self):
        s = CycleSupervisor(settings=ResilienceSettings(base_backoff=5, max_backoff=10))
        s.mark_failure(ValueError("boom"))
        s.mark_failure(ValueError("boom"))
        s.mark_failure(ValueError("boom"))
        s.mark_failure(ValueError("boom"))
        # Would be 20 but capped at 10
        assert s.mark_failure(ValueError("boom")) == 10.0
