"""把证据、映射复核与已发说明整理成可交付的柜台建议和目录调整清单。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Mapping, Sequence

from .advice import AdviceReview, alias_impact, review_sent_advice
from .aliases import AliasResolver
from .baskets import Observation, build_observations
from .catalog import Catalog, CombinationCheck
from .evidence import (
    SUPPORTED,
    EvidenceConfig,
    PairEvidence,
    analyze_scopes,
    pending_mapping_impact,
)

ADVISORY = "remind-and-verify"
ACTIONABLE = "actionable-guidance"
DEFER = "defer"


@dataclass
class Recommendation:
    pair: str
    evidence: PairEvidence
    kind: str
    scope_sites: tuple[str, ...]
    audience: tuple[str, ...]
    check: CombinationCheck | None
    reasons: tuple[str, ...]
    counterexamples: Mapping[str, int]
    coverage_gaps: tuple[str, ...]
    advisory_note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "pair": self.pair,
            "kind": self.kind,
            "scope_sites": list(self.scope_sites),
            "audience": list(self.audience),
            "evidence": self.evidence.as_dict(),
            "check": None
            if self.check is None
            else {
                "actionable": self.check.actionable,
                "separately_applicable": self.check.separately_applicable,
                "eligibility_ok": self.check.eligibility_ok,
                "order": list(self.check.order),
                "blockers": list(self.check.blockers),
                "notes": list(self.check.notes),
                "audience": self.check.audience,
            },
            "reasons": list(self.reasons),
            "counterexamples": dict(self.counterexamples),
            "coverage_gaps": list(self.coverage_gaps),
            "advisory_note": self.advisory_note,
        }


def build_report(
    tables: Mapping[str, list[dict[str, str]]],
    *,
    today: str,
    per_site: bool = True,
    config: EvidenceConfig | None = None,
) -> dict[str, object]:
    resolver = AliasResolver(tables)
    catalog = Catalog(tables)
    observations = build_observations(tables, resolver=resolver, catalog=catalog)
    config = config or EvidenceConfig()

    scopes: list[str | None] = [None]
    if per_site:
        scopes.extend(sorted({observation.site for observation in observations}))
    evidence_by_scope = analyze_scopes(observations, catalog, scopes=scopes, config=config)
    recommendations = _recommendations(evidence_by_scope, catalog, observations, today)
    advice_reviews = review_sent_advice(tables, today=today, resolver=resolver, catalog=catalog)
    mapping_impact = pending_mapping_impact(observations, resolver)
    alias_impacts = {
        row.id: alias_impact(row.id, tables, resolver=resolver, observations=observations)
        for row in resolver.rows
        if row.state in {"reviewed_correction", "ambiguous"}
    }

    return {
        "today": today,
        "summary": _summary(observations, evidence_by_scope, recommendations, advice_reviews),
        "evidence": {
            scope: [evidence.as_dict() for evidence in items]
            for scope, items in evidence_by_scope.items()
        },
        "recommendations": [item.as_dict() for item in recommendations],
        "mapping_scope": resolver.scope_report(),
        "pending_mapping_impact": mapping_impact,
        "alias_impacts": alias_impacts,
        "advice": {
            "current": [_review_dict(r) for r in advice_reviews if r.bucket == "current"],
            "sent": [_review_dict(r) for r in advice_reviews if r.bucket == "sent"],
            "clarify": [_review_dict(r) for r in advice_reviews if r.bucket == "clarify"],
            "sent_originals": [_review_dict(r) for r in advice_reviews],
        },
        "data_issues": _data_issues(observations),
    }


def _recommendations(
    scopes: Mapping[str, list[PairEvidence]],
    catalog: Catalog,
    observations: Sequence[Observation],
    today: str,
) -> list[Recommendation]:
    merged: dict[tuple[str, tuple[str, ...]], Recommendation] = {}
    # 站点范围更具体，先处理，避免全地区结果重复覆盖同一批站点建议。
    ordered = sorted(scopes.items(), key=lambda item: item[0] == "region")
    for scope, items in ordered:
        for evidence in items:
            if evidence.classification != SUPPORTED:
                continue
            candidate = _recommend(evidence, scope, catalog, observations, today)
            key = (candidate.pair, candidate.scope_sites)
            existing = merged.get(key)
            if existing is None:
                merged[key] = candidate
            else:
                merged[key] = _merge(existing, candidate)
    return list(merged.values())


def _recommend(
    evidence: PairEvidence,
    scope: str,
    catalog: Catalog,
    observations: Sequence[Observation],
    today: str,
) -> Recommendation:
    pair_codes = (evidence.a, evidence.b)
    eligible_groups = _current_eligible_groups(catalog, pair_codes, today)
    evidence_groups = tuple(sorted(evidence.groups_joint)) or tuple(sorted(eligible_groups))
    groups = tuple(group for group in evidence_groups if group in eligible_groups)
    household = _representative_household(observations, catalog, groups)
    check = catalog.check_combination(pair_codes, household, today) if household else None

    reasons: list[str] = []
    gaps: list[str] = []
    kind = ACTIONABLE

    if not eligible_groups:
        kind = DEFER
        reasons.append("按当前目录，两项已没有共同可办理的家庭组")
    elif not groups:
        # 证据里的共同选择全部来自当前不能办理的家庭组：只能作为核实线索。
        kind = ADVISORY
        audit = "共同选择记录全部来自当前不具备办理资格的家庭组"
        reasons.append(audit)
        for code in pair_codes:
            entry = catalog.entry_for(code, today)
            if entry is not None:
                reasons.append(f"{code} 当前仅限 {entry.eligibility}")
    elif check is None:
        kind = DEFER
        reasons.append("样本中没有可用于资格试算的家庭")
    elif evidence.fragile:
        kind = DEFER
        reasons.append("证据对资格范围、全选篮子或套餐默认的处理敏感，暂不足以放入柜台建议")
        for item in evidence.sensitivities:
            if item.classification != evidence.classification:
                reasons.append(
                    f"{item.label}后为 {item.classification}"
                    f"（适用 {item.applicable} 户，共同选择 {item.joint} 户）"
                )
    elif check.advisory_only:
        kind = ADVISORY
        reasons.append("组合含互斥服务，只能用于提醒顾问核实实际情况")
    elif not check.actionable:
        kind = DEFER
        reasons.extend(check.blockers)
    else:
        if check.order:
            reasons.append("办理顺序：" + " → ".join(check.order))
        if not check.separately_applicable:
            gaps.append("当前不能分开申请，提交前需确认受理方式")

    # 只在部分站点有共同选择记录时，推荐范围限于这些站点。
    if scope == "region":
        if len(evidence.sites_joint) < len(evidence.sites_offered):
            gaps.append(
                "只在 " + ",".join(evidence.sites_joint) + " 有共同选择记录；"
                "不能继承为全地区推荐"
            )
            scope_sites = tuple(evidence.sites_joint)
        else:
            scope_sites = tuple(evidence.sites_joint)
    else:
        scope_sites = (scope,)
        other_sites = [site for site in evidence.sites_offered if site != scope]
        if other_sites:
            gaps.append("其他站点（" + ",".join(other_sites) + "）没有纳入本范围证据")

    if evidence.applicable < 20:
        gaps.append(f"适用样本仅 {evidence.applicable} 次接洽，属于覆盖不足")
    if evidence.not_offered:
        reasons.append(f"{evidence.not_offered} 户未展示该项目，没有选择机会，未作为反例")
    if evidence.bundle_default_joint:
        gaps.append("部分同时选择来自套餐默认，需与居民另选分开解读")
    if evidence.excluded_pending:
        gaps.append(f"{evidence.excluded_pending} 户因映射待判断未纳入")
    if evidence.ineligible_joint:
        gaps.append(
            f"共同选择中有 {evidence.ineligible_joint} 户按当日目录不具备其中一项资格，"
            "不能直接当作可办理组合"
        )

    counterexamples = {
        "only_a": evidence.only_a,
        "only_b": evidence.only_b,
        "neither": evidence.neither,
        "not_offered": evidence.not_offered,
    }
    advisory_note = ""
    if kind == ADVISORY:
        advisory_note = "以“请顾问核实”措辞发送，不写成可照做的联合办理步骤"
    return Recommendation(
        pair=f"{evidence.a}+{evidence.b}",
        evidence=evidence,
        kind=kind,
        scope_sites=scope_sites,
        audience=groups,
        check=check,
        reasons=tuple(dict.fromkeys(reasons)),
        counterexamples=counterexamples,
        coverage_gaps=tuple(dict.fromkeys(gaps)),
        advisory_note=advisory_note,
    )


_KIND_ORDER = {ACTIONABLE: 0, ADVISORY: 1, DEFER: 2}


def _merge(left: Recommendation, right: Recommendation) -> Recommendation:
    """同一组合、同一范围出现多个结论时取最保守的一个，并合并说明。"""
    conservative = left if _KIND_ORDER[left.kind] >= _KIND_ORDER[right.kind] else right
    other = right if conservative is left else left
    # 结论取最保守的一份；另一个范围的说明加范围前缀后并列，避免误读。
    label = "全地区" if other.evidence.scope == "region" else other.evidence.scope
    other_reasons = tuple(
        f"{label}范围：{reason}"
        for reason in other.reasons
        if "未展示该项目" not in reason
    )
    other_gaps = tuple(
        f"{label}范围：{gap}"
        for gap in other.coverage_gaps
        if gap not in conservative.coverage_gaps
    )
    return Recommendation(
        pair=conservative.pair,
        evidence=conservative.evidence,
        kind=conservative.kind,
        scope_sites=conservative.scope_sites,
        audience=conservative.audience,
        check=conservative.check,
        reasons=tuple(dict.fromkeys(conservative.reasons + other_reasons)),
        counterexamples=conservative.counterexamples,
        coverage_gaps=tuple(dict.fromkeys(conservative.coverage_gaps + other_gaps)),
        advisory_note=conservative.advisory_note,
    )


def _representative_household(
    observations: Sequence[Observation],
    catalog: Catalog,
    groups: Sequence[str],
) -> str:
    wanted = set(groups)
    for observation in observations:
        if observation.own and catalog.group_of(observation.household) in wanted:
            return observation.household
    return ""


def _current_eligible_groups(
    catalog: Catalog, codes: Sequence[str], day: str
) -> set[str]:
    groups: set[str] | None = None
    for code in codes:
        entry = catalog.entry_for(code, day)
        if entry is None:
            return set()
        allowed = (
            {"general", "support"}
            if entry.eligibility == "general"
            else {entry.eligibility}
        )
        groups = allowed if groups is None else groups & allowed
    return groups or set()


def _summary(
    observations: Sequence[Observation],
    scopes: Mapping[str, Sequence[PairEvidence]],
    recommendations: Sequence[Recommendation],
    advice_reviews: Sequence[AdviceReview],
) -> dict[str, object]:
    counts = Counter(review.bucket for review in advice_reviews)
    return {
        "observations": len(observations),
        "pending_mapping_observations": sum(1 for o in observations if o.pending),
        "shared_household_observations": sum(1 for o in observations if o.shared),
        "automatic_defaults": sum(len(o.automatic) for o in observations),
        "withdrawn_lines": sum(len(o.withdrawn) for o in observations),
        "scopes": {scope: len(items) for scope, items in scopes.items()},
        "supported": sum(
            1
            for items in scopes.values()
            for evidence in items
            if evidence.classification == SUPPORTED
        ),
        "recommendations": {
            "actionable": sum(1 for item in recommendations if item.kind == ACTIONABLE),
            "advisory": sum(1 for item in recommendations if item.kind == ADVISORY),
            "defer": sum(1 for item in recommendations if item.kind == DEFER),
        },
        "advice": {
            "current": counts["current"],
            "sent": counts["sent"],
            "clarify": counts["clarify"],
            "sent_originals": len(advice_reviews),
        },
    }


def _review_dict(review: AdviceReview) -> dict[str, object]:
    return review.as_dict()


def _data_issues(observations: Sequence[Observation]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    bundle_totals = [
        {
            "session": observation.session,
            "household": observation.household,
            "parents": sorted(observation.unexpanded_parents),
        }
        for observation in observations
        if observation.unexpanded_parents
    ]
    if bundle_totals:
        issues.append(
            {
                "kind": "bundle-total-only",
                "count": len(bundle_totals),
                "sessions": [item["session"] for item in bundle_totals],
                "detail": "这些接洽只登记了套餐总项；分析时保留总项，未反推子项",
            }
        )
    for observation in observations:
        if observation.shared:
            issues.append(
                {
                    "kind": "shared-household",
                    "session": observation.session,
                    "household": observation.household,
                    "detail": "同一会话为另一家庭登记，未与该会话主家庭合并",
                }
            )
        if observation.pending:
            issues.append(
                {
                    "kind": "pending-mapping",
                    "session": observation.session,
                    "household": observation.household,
                    "candidates": sorted(observation.pending_candidates),
                }
            )
        if observation.eligibility_conflicts:
            issues.append(
                {
                    "kind": "eligibility",
                    "session": observation.session,
                    "household": observation.household,
                    "codes": list(observation.eligibility_conflicts),
                }
            )
        for message in observation.issues:
            if (
                "重复登记" in message
                or "不在当次展示" in message
                or "未知确认状态" in message
            ):
                issues.append(
                    {
                        "kind": "line-quality",
                        "session": observation.session,
                        "household": observation.household,
                        "detail": message,
                    }
                )
    return issues
