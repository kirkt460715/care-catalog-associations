"""组合证据、映射复核和已发说明的业务规则。"""

from __future__ import annotations

import unittest

from service_evidence import load_tables
from service_evidence.advice import alias_impact, review_sent_advice
from service_evidence.aliases import AliasResolver, MISMATCH, PENDING, RESOLVED
from service_evidence.baskets import build_observations, dedupe_by_household
from service_evidence.catalog import Catalog
from service_evidence.evidence import (
    SUPPORTED,
    EvidenceConfig,
    analyze_scopes,
    pending_mapping_impact,
)
from service_evidence.report import build_report


class AliasScopeTests(unittest.TestCase):
    def setUp(self):
        self.tables = load_tables()
        self.resolver = AliasResolver(self.tables)

    def test_same_name_is_scoped_by_site(self):
        e = self.resolver.resolve("上门协助", "E", "2026-04-22")
        w = self.resolver.resolve("上门协助", "W", "2026-04-22")
        self.assertEqual((e.status, e.canonical), (RESOLVED, "clean"))
        self.assertEqual((w.status, w.canonical), (RESOLVED, "lift"))

    def test_conflicting_rows_stay_pending_with_candidates(self):
        resolution = self.resolver.resolve("上门协助", "N", "2026-04-22", "clean")
        self.assertEqual(resolution.status, PENDING)
        self.assertEqual(resolution.candidates, frozenset({"clean", "lift"}))

    def test_reviewed_correction_wins_in_its_own_window(self):
        resolution = self.resolver.resolve("上门协助", "N", "2026-02-01", "clean")
        self.assertEqual(resolution.status, RESOLVED)
        self.assertEqual(resolution.canonical, "clean")
        self.assertIn("A1", resolution.superseded)

    def test_observed_id_conflict_is_not_used_as_resolved(self):
        resolution = self.resolver.resolve("上门协助", "E", "2026-04-22", "lift")
        self.assertEqual(resolution.status, MISMATCH)

    def test_pending_windows_are_limited_to_pending_periods(self):
        windows = self.resolver.pending_windows()
        self.assertEqual(len(windows), 1)
        self.assertEqual((windows[0]["site"], windows[0]["start"], windows[0]["end"]), ("N", "2026-04-01", "2026-05-12"))


class BasketTests(unittest.TestCase):
    def setUp(self):
        self.tables = load_tables()
        self.observations = build_observations(self.tables)

    def test_shared_session_is_split_by_household(self):
        shared = [o for o in self.observations if o.shared]
        self.assertEqual({(o.session, o.household) for o in shared}, {("C085", "H086"), ("C102", "H103")})
        self.assertNotIn("escape", next(o for o in shared if o.household == "H086").chosen)

    def test_bundle_default_is_separate_from_resident_choice(self):
        observation = next(o for o in self.observations if o.session == "C024" and o.household == "H024")
        self.assertEqual(observation.chosen, frozenset({"bath", "escort", "bundle"}))
        self.assertEqual(observation.automatic, frozenset({"clean"}))
        self.assertNotIn("clean", observation.selected())
        self.assertIn("clean", observation.selected(include_automatic=True))

    def test_retraction_is_not_counted_as_purchase(self):
        observation = next(o for o in self.observations if o.session == "C066")
        self.assertFalse(observation.chosen)
        self.assertEqual(observation.withdrawn, frozenset({"escape", "clean", "escort", "lift", "bundle"}))

    def test_bundle_total_is_not_expanded(self):
        observation = next(o for o in self.observations if o.session == "C024" and o.household == "H024")
        # C024 的套餐总项带有自动子项记录，属于已展开；未展开的套餐不得补造子项。
        self.assertEqual(observation.defaulted_by_parent, {"bundle": frozenset({"clean"})})
        self.assertNotIn("lift", observation.chosen)
        bare = next(o for o in self.observations if o.session == "C130")
        self.assertEqual(bare.unexpanded_parents, frozenset({"bundle"}))
        self.assertEqual(bare.chosen, frozenset({"bundle"}))
        self.assertEqual(bare.automatic, frozenset())

    def test_dedupe_keeps_one_observation_per_household(self):
        deduped = dedupe_by_household(self.observations)
        self.assertEqual(len(deduped), len({unit.household for unit in deduped}))
        self.assertLess(len(deduped), len([o for o in self.observations if o.own]))

    def test_dedupe_prefers_home_site(self):
        deduped = {unit.household: unit for unit in dedupe_by_household(self.observations)}
        # H103 有本人接洽，取本人那一次，而不是借用会话里的登记。
        self.assertEqual(deduped["H103"].observation.session, "C103")
        self.assertFalse(deduped["H103"].observation.shared)
        self.assertEqual(
            [repeat.session for repeat in deduped["H024"].repeats], ["C134"]
        )


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog(load_tables())

    def test_versions_are_time_aware(self):
        self.assertTrue(self.catalog.entry_for("lift", "2026-05-01").independent)
        self.assertFalse(self.catalog.entry_for("lift", "2026-07-01").independent)

    def test_requires_and_order(self):
        check = self.catalog.check_combination(["bath", "lift"], "H017", "2026-07-01")
        self.assertTrue(check.actionable)
        self.assertEqual(check.order, ("lift", "bath"))
        self.assertFalse(self.catalog.check_combination(["bath"], "H017", "2026-07-01").actionable)

    def test_mutually_exclusive_is_advisory_only(self):
        check = self.catalog.check_combination(["escape", "lift"], "H001", "2026-05-01")
        self.assertFalse(check.actionable)
        self.assertTrue(check.advisory_only)

    def test_eligibility_is_group_and_date_aware(self):
        self.assertTrue(self.catalog.eligible("lift", "H017", "2026-05-01"))
        self.assertTrue(self.catalog.eligible("lift", "H017", "2026-07-01"))
        self.assertFalse(self.catalog.eligible("lift", "H003", "2026-05-01"))
        self.assertTrue(self.catalog.eligible("clean", "H003", "2026-05-01"))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tables = load_tables()
        self.catalog = Catalog(self.tables)
        self.observations = build_observations(self.tables, catalog=self.catalog)
        self.results = analyze_scopes(
            self.observations, self.catalog, scopes=[None, "E", "N", "W"]
        )

    def test_saturated_all_option_baskets_do_not_become_supported(self):
        evidence = next(
            item for item in self.results["N"] if {item.a, item.b} == {"bundle", "escort"}
        )
        self.assertGreater(evidence.saturated, 0)
        self.assertNotEqual(evidence.classification, SUPPORTED)
        self.assertTrue(evidence.saturated >= evidence.joint_households // 2)

    def test_conditional_test_does_not_use_unshown_households(self):
        evidence = next(
            item for item in self.results["N"] if {item.a, item.b} == {"bath", "lift"}
        )
        self.assertGreater(evidence.not_offered, 0)
        self.assertEqual(evidence.applicable + evidence.not_offered, evidence.households + evidence.not_offered)
        self.assertLess(evidence.applicable, evidence.households + evidence.not_offered)

    def test_pending_mapping_is_excluded_only_from_its_own_pairs(self):
        impact = pending_mapping_impact(self.observations, AliasResolver(self.tables))
        self.assertEqual(len(impact), 1)
        self.assertEqual(impact[0]["candidates"], ["clean", "lift"])
        # 待判断的用语只在涉及 clean/lift 的组合里被剔除。
        exposure = next(
            item for item in self.results["N"] if {item.a, item.b} == {"clean", "escort"}
        )
        self.assertGreater(exposure.excluded_pending, 0)
        unaffected = next(
            item for item in self.results["W"] if {item.a, item.b} == {"bath", "bundle"}
        )
        self.assertEqual(unaffected.excluded_pending, 0)

    def test_sensitivity_flags_fragile_pair(self):
        evidence = next(
            item for item in self.results["N"] if {item.a, item.b} == {"bath", "lift"}
        )
        self.assertTrue(evidence.fragile)
        self.assertTrue(any("资格" in w or "敏感" in w for w in evidence.warnings))


class AdviceTests(unittest.TestCase):
    def setUp(self):
        self.tables = load_tables()
        self.reviews = {review.record.id: review for review in review_sent_advice(
            self.tables, today="2026-11-22"
        )}

    def test_buckets_keep_originals(self):
        self.assertEqual(self.reviews["T3"].bucket, "current")
        self.assertEqual(self.reviews["T2"].bucket, "sent")
        self.assertEqual(self.reviews["T1"].bucket, "clarify")

    def test_stopped_standalone_item_keeps_continuity(self):
        review = self.reviews["T1"]
        self.assertTrue(any("停止单办" in finding for finding in review.findings))
        self.assertTrue(any("已于 2026-04-22 受理" in note for note in review.continuity))
        self.assertTrue(any("新请求按当下目录" in note for note in review.continuity))

    def test_alias_impact_does_not_cross_sites(self):
        impact = alias_impact("A6", self.tables)
        self.assertEqual(impact["site"], "N")
        self.assertEqual(set(impact["unaffected_same_name_sites"]), {"E", "W"})
        self.assertEqual(impact["affected_advice"], ["T5"])

    def test_ambiguous_source_is_clarified_not_deleted(self):
        review = self.reviews["T1"]
        self.assertEqual(review.record.services, ("lift", "bath"))
        self.assertIn("A2", review.affected_aliases)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.report = build_report(load_tables(), today="2026-11-22")

    def test_recommendations_carry_scope_and_gaps(self):
        self.assertTrue(self.report["recommendations"])
        for item in self.report["recommendations"]:
            self.assertNotIn(item["kind"], ("actionable-guidance",))
            self.assertTrue(item["coverage_gaps"] or item["reasons"])
            self.assertNotEqual(item["scope_sites"], ["region"])

    def test_advice_entry_has_three_sections(self):
        self.assertEqual(set(self.report["advice"]), {"current", "sent", "clarify", "sent_originals"})
        self.assertEqual(len(self.report["advice"]["sent_originals"]), 5)

    def test_shared_households_are_reported(self):
        kinds = [issue["kind"] for issue in self.report["data_issues"]]
        self.assertIn("shared-household", kinds)


if __name__ == "__main__":
    unittest.main()
