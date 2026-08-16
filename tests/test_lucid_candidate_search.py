"""Regression tests for the pre-registered LucidPro 25K candidate search.

Several of these fail against the implementation that existed before this
change: the artifact fingerprint test fails against the stale byte-hashed
manifest, and the pool-diversity test fails against the frequency-only pool
rule that filled every slot with one family.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TA_STRAT = ROOT / "research" / "ta_strat"
if str(TA_STRAT) not in sys.path:
    sys.path.insert(0, str(TA_STRAT))

import lucid_candidate_search as S  # noqa: E402
import lucid_lab_validation as V  # noqa: E402


ARTIFACT = TA_STRAT / "results" / "lucid_lab_validation.json"
SEARCH_ARTIFACT = TA_STRAT / "results" / "lucid_candidate_search.json"


def _sessions(count: int, start: date = date(2016, 7, 1)) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


class TestPreRegistration:
    def test_fingerprint_is_stable_and_covers_the_whole_grid(self):
        assert S.PRE_REGISTRATION_SHA == hashlib.sha256(
            S._preregistration_payload().encode()
        ).hexdigest()[:16]
        payload = json.loads(S._preregistration_payload())
        assert {row["family"] for row in payload["families"]} == {
            item.family for item in S.PRE_REGISTRATION
        }

    def test_editing_the_grid_changes_the_fingerprint(self, monkeypatch):
        original = S.PRE_REGISTRATION_SHA
        monkeypatch.setattr(
            S, "PRE_REGISTRATION", S.PRE_REGISTRATION[:-1], raising=True
        )
        assert hashlib.sha256(
            S._preregistration_payload().encode()
        ).hexdigest()[:16] != original

    def test_state_refuses_to_run_after_the_grid_moves(self, monkeypatch):
        state = S.SearchState(preregistration_sha="0000000000000000")
        with pytest.raises(RuntimeError, match="fingerprint changed"):
            state.check()

    def test_every_family_declares_an_economic_rationale(self):
        for item in S.PRE_REGISTRATION:
            assert len(item.rationale.split()) >= 8, item.family
            assert item.grid, item.family

    def test_untestable_families_are_excluded_with_a_reason(self):
        excluded = {row["family"] for row in S.EXCLUDED_FAMILIES}
        assert "vwap_reversion" in excluded
        assert "event_filtered_breakout" in excluded
        for row in S.EXCLUDED_FAMILIES:
            assert row["reason"].strip()


class TestPoolDiversity:
    """Fails against the superseded frequency-only pool rule."""

    def _ranked(self):
        return [
            {"candidate": "es:trend_pullback_a", "family": "trend_pullback", "market": "es"},
            {"candidate": "es:trend_pullback_b", "family": "trend_pullback", "market": "es"},
            {"candidate": "es:trend_pullback_c", "family": "trend_pullback", "market": "es"},
            {"candidate": "nq:prior_breakout_a", "family": "prior_breakout", "market": "nq"},
            {"candidate": "nq:prior_breakout_b", "family": "prior_breakout", "market": "nq"},
            {"candidate": "es:or_break_a", "family": "or_break", "market": "es"},
        ]

    def test_pool_keeps_one_sleeve_per_family_and_market(self):
        pool = S.select_pool(self._ranked(), pool_size=4)
        assert pool == [
            "es:trend_pullback_a",
            "nq:prior_breakout_a",
            "es:or_break_a",
        ]

    def test_old_frequency_only_rule_would_have_been_degenerate(self):
        degenerate = S.select_pool(
            self._ranked(), pool_size=3, one_per_family_market=False
        )
        assert len({name.split(":")[1].rsplit("_", 1)[0] for name in degenerate}) == 1

    def test_pool_never_exceeds_requested_size(self):
        assert len(S.select_pool(self._ranked(), pool_size=2)) == 2


class TestMultiplicity:
    def test_second_holdout_opening_is_paid_for(self):
        per_run = S.len_single = len(S.single_variants()) + S.portfolio_combination_count()
        assert S.total_variants_tested() == per_run * S.HOLDOUT_OPENINGS
        assert S.HOLDOUT_OPENINGS >= 2

    def test_sidak_alpha_tightens_with_more_variants(self):
        assert S.sidak_alpha(1) == pytest.approx(0.05)
        assert S.sidak_alpha(282) < S.sidak_alpha(10) < 0.05

    def test_corrected_interval_is_wider_than_the_raw_interval(self):
        raw = V._clopper_pearson(3, 13)
        corrected = V._clopper_pearson(3, 13, alpha=S.sidak_alpha(282))
        assert corrected[0] < raw[0] and corrected[1] > raw[1]

    def test_best_of_n_null_rises_with_the_number_of_variants(self):
        few = S.best_of_n_null_expectation(12, 1)
        many = S.best_of_n_null_expectation(12, 282)
        assert 0.0 < few < many <= 1.0
        assert many > 0.75


class TestHoldoutLock:
    def test_holdout_may_only_be_opened_once(self):
        state = S.SearchState()
        state.holdout_opened_for = "first_candidate"
        with pytest.raises(RuntimeError, match="already opened"):
            state.lock_holdout("second_candidate", [], S.Split("holdout", ()), None)

    def test_a_split_without_a_full_block_is_refused(self):
        short = S.Split("holdout", tuple(_sessions(10)))
        assert short.blocks == 0
        with pytest.raises(ValueError, match="no complete"):
            S.score([], short, None)


class TestSplitsAndPrecision:
    def test_splits_are_chronological_and_disjoint(self):
        sessions = _sessions(2000, date(2016, 7, 1))
        splits = S.build_splits(sessions)
        dev, val, hold = splits["development"], splits["validation"], splits["holdout"]
        assert max(dev.sessions) <= S.DEVELOPMENT_END < min(val.sessions)
        assert max(val.sessions) <= S.VALIDATION_END < min(hold.sessions)
        assert not (set(dev.sessions) & set(val.sessions) & set(hold.sessions))

    def test_blocks_are_whole_disjoint_horizons(self):
        split = S.Split("x", tuple(_sessions(100)))
        assert split.blocks == 100 // S.HORIZON

    def test_precision_ceiling_is_reported_and_too_wide_for_a_decision(self):
        split = S.Split("holdout", tuple(_sessions(588)))
        assert split.blocks == 13
        assert split.precision_halfwidth() > 0.25

    def test_holdout_is_not_claimed_pristine(self):
        assert S.HOLDOUT_IS_PRISTINE is False


class TestBaskets:
    def test_zero_trade_sessions_stay_in_the_denominator(self):
        sessions = _sessions(5)
        baskets = S.daily_baskets([], sessions)
        assert len(baskets) == len(sessions)
        assert all(basket == [] for basket in baskets)

    def test_trades_outside_the_split_are_dropped_not_reassigned(self):
        sessions = _sessions(3)

        class FakeTrade:
            def __init__(self, day):
                self.day = day
                self.entry_ts = 0
                self.exit_ts = 1
                self.market = "es"
                self.strategy = "x"

        baskets = S.daily_baskets([FakeTrade(date(1999, 1, 4))], sessions)
        assert sum(len(basket) for basket in baskets) == 0


class TestBenchmarksAndVerdict:
    def test_cash_benchmark_never_passes_or_breaches(self):
        split = S.Split("holdout", tuple(_sessions(588)))
        cash = S.cash_benchmark(split)
        assert cash["pass_rate"] == 0.0
        assert cash["breach_rate"] == 0.0
        assert cash["unfinished_rate"] == 1.0
        assert cash["windows"] == split.blocks

    def test_verdict_is_fail_closed_and_never_enables_trading(self):
        report = {
            "data": {"exchange_grade": False},
            "holdout_pristine": False,
            "event_filter_applied": False,
            "holdout": {"pass_rate": 0.9, "pass_multiplicity_95": [0.0, 1.0]},
            "baseline": {"holdout": {"pass_rate": 0.1}},
            "best_of_n_null_pass_rate": 0.86,
            "variants_tested": 282,
            "stresses": [{"breach_rate": 0.0}],
        }
        verdict = S.decide(report)
        assert verdict["decision"] == "NO_GO"
        assert verdict["auto_trade_allowed"] is False

    def test_a_flattering_pass_rate_cannot_clear_the_precision_gate(self):
        report = {
            "data": {"exchange_grade": True},
            "holdout_pristine": True,
            "event_filter_applied": True,
            "holdout": {"pass_rate": 0.95, "pass_multiplicity_95": [0.20, 0.99]},
            "baseline": {"holdout": {"pass_rate": 0.10}},
            "best_of_n_null_pass_rate": 0.5,
            "variants_tested": 282,
            "stresses": [{"breach_rate": 0.0}],
        }
        verdict = S.decide(report)
        assert verdict["decision"] == "NO_GO"
        assert "decision_precision" in verdict["failed_gates"]

    def test_missing_stresses_cannot_silently_pass_the_stress_gate(self):
        report = {
            "data": {"exchange_grade": True},
            "holdout_pristine": True,
            "event_filter_applied": True,
            "holdout": {"pass_rate": 0.9, "pass_multiplicity_95": [0.85, 0.95]},
            "baseline": {"holdout": {"pass_rate": 0.1}},
            "best_of_n_null_pass_rate": 0.5,
            "variants_tested": 1,
            "stresses": [],
        }
        assert S.decide(report)["decision"] == "NO_GO"


@pytest.mark.skipif(not SEARCH_ARTIFACT.exists(), reason="search artifact not generated")
class TestSearchArtifact:
    def _load(self):
        with SEARCH_ARTIFACT.open(encoding="utf-8") as handle:
            return json.load(handle)

    def test_artifact_matches_the_current_pre_registration(self):
        assert self._load()["preregistration_sha"] == S.PRE_REGISTRATION_SHA

    def test_headline_uses_disjoint_blocks_only(self):
        data = self._load()
        assert data["holdout"]["windows_overlap"] is False
        assert data["holdout"]["window_stride_sessions"] == S.HORIZON

    def test_outcomes_partition_every_block(self):
        data = self._load()
        for row in [data["holdout"], *data["validation"]]:
            assert row["passes"] + row["breaches"] + row["unfinished"] == row["windows"]

    def test_no_candidate_beat_the_frozen_baseline(self):
        data = self._load()
        assert data["holdout"]["pass_rate"] <= data["baseline"]["holdout"]["pass_rate"]

    def test_decision_is_no_go_and_records_every_failed_gate(self):
        verdict = self._load()["verdict"]
        assert verdict["decision"] == "NO_GO"
        assert "exchange_grade_data" in verdict["failed_gates"]
        assert "decision_precision" in verdict["failed_gates"]
        assert verdict["auto_trade_allowed"] is False

    def test_event_filter_is_reported_as_not_applied(self):
        data = self._load()
        assert data["event_filter_applied"] is False
        assert "calendar" in data["event_filter_note"].lower()

    def test_data_is_labelled_non_decision_grade(self):
        data = self._load()["data"]
        assert data["exchange_grade"] is False
        assert data["decision_grade"] is False


@pytest.mark.skipif(not ARTIFACT.exists(), reason="validation artifact not generated")
class TestImplementationFingerprint:
    """Fails against the stale byte-hashed manifest committed in PR #41."""

    def _load(self):
        with ARTIFACT.open(encoding="utf-8") as handle:
            return json.load(handle)

    def test_every_source_fingerprint_reproduces(self):
        for row in self._load()["implementation_manifest"]:
            path = TA_STRAT / row["name"]
            assert path.exists(), row["name"]
            assert V._hash_source(path) == row["sha256"], (
                f"{row['name']} fingerprint is stale; regenerate the artifact"
            )

    def test_source_hash_ignores_checkout_line_endings(self, tmp_path):
        lf, crlf = tmp_path / "lf.py", tmp_path / "crlf.py"
        lf.write_bytes(b"a = 1\nb = 2\n")
        crlf.write_bytes(b"a = 1\r\nb = 2\r\n")
        assert V._hash_source(lf) == V._hash_source(crlf)

    def test_byte_hash_still_distinguishes_untracked_data(self, tmp_path):
        one, two = tmp_path / "a.csv", tmp_path / "b.csv"
        one.write_bytes(b"x\n")
        two.write_bytes(b"y\n")
        assert V._hash_file(one) != V._hash_file(two)

    def test_run_id_is_self_consistent(self):
        data = self._load()
        claimed = data.pop("run_id")
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"))
        assert hashlib.sha256(raw.encode()).hexdigest()[:16] == claimed

    def test_artifact_carries_the_candidate_search_headline(self):
        summary = self._load()["candidate_search"]
        assert summary["beat_frozen_baseline"] is False
        assert summary["decision"] == "NO_GO"
        assert summary["variants_tested"] == S.total_variants_tested()
