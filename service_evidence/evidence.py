"""组合证据：区分“常同时出现”与“在相同备选集合下仍有共同选择”。

方法
----
把每户一次接洽视为一个观察。只有当两项目都在该次展示清单里时，才进入分母；
从未展示过的家庭没有展示机会，不作为反例。

在同一分层（站点 × 渠道 × 展示清单 × 期间）内，若两项各自独立选择，
两项同时被选的数量服从超几何分布。把各层分布卷积得到总重叠的单侧 p 值，
再对全地区与各站点的全部候选组合统一做 Holm 校正。

稳健性
------
同一个组合会做三项敏感性检查，任一项改变结论就标为脆弱：

1. 只保留按当日目录对两项都合格的家庭；
2. 去掉“当次展示全选”的篮子；
3. 把套餐默认带出的项目并入选择。

``automatic`` 默认不计入居民选择；``chosen`` 才是另选。
映射待判断的条目只从涉及它的组合中剔除，不影响该次接洽的其他项目。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from math import comb
from typing import Iterable, Mapping, Sequence

from .baskets import HouseholdObservation, Observation, dedupe_by_household
from .catalog import Catalog

SUPPORTED = "supported-joint-selection"
NOMINAL = "nominal-signal-unconfirmed"
CO_OCCURRENCE_ONLY = "frequent-co-occurrence-only"
INSUFFICIENT = "insufficient-support"
NO_EVIDENCE = "no-selection-evidence"


@dataclass(frozen=True)
class EvidenceConfig:
    alpha: float = 0.05
    min_applicable: int = 10
    min_joint_households: int = 5
    min_sites: int = 1
    include_automatic: bool = False
    require_eligibility: bool = False
    period: bool = False
    robustness: bool = True

    def stratum_key(self, observation: Observation, scope: str | None) -> tuple[str, ...]:
        parts = [observation.channel, ",".join(sorted(observation.displayed))]
        if scope is None:
            parts.append(observation.site)
        if self.period:
            parts.append(observation.day[:7])
        return tuple(parts)


@dataclass
class Sensitivity:
    label: str
    applicable: int
    joint: int
    p_value: float
    classification: str

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "applicable": self.applicable,
            "joint": self.joint,
            "p_value": round(self.p_value, 4),
            "classification": self.classification,
        }


@dataclass
class PairEvidence:
    a: str
    b: str
    scope: str
    period: str
    applicable: int
    households: int
    joint: int
    joint_households: int
    only_a: int
    only_b: int
    neither: int
    expected_joint: float
    p_value: float
    p_adjusted: float
    strata: int
    sites_offered: tuple[str, ...]
    sites_joint: tuple[str, ...]
    groups_offered: Mapping[str, int]
    groups_joint: Mapping[str, int]
    not_offered: int
    excluded_pending: int
    bundle_default_joint: int
    eligibility_conflicts: int
    ineligible_joint: int
    saturated: int
    classification: str = NO_EVIDENCE
    rank: int | None = None
    warnings: tuple[str, ...] = ()
    sensitivities: tuple[Sensitivity, ...] = ()

    @property
    def lift(self) -> float:
        return self.joint / self.expected_joint if self.expected_joint else 0.0

    @property
    def nominal_signal(self) -> bool:
        return self.p_value <= 0.05 < self.p_adjusted

    @property
    def fragile(self) -> bool:
        if self.classification not in {SUPPORTED, NOMINAL}:
            return False
        return any(
            item.classification != self.classification for item in self.sensitivities
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "pair": f"{self.a}+{self.b}",
            "scope": self.scope,
            "period": self.period or "全期间",
            "applicable": self.applicable,
            "households": self.households,
            "joint": self.joint,
            "joint_households": self.joint_households,
            "only_a": self.only_a,
            "only_b": self.only_b,
            "neither": self.neither,
            "expected_joint": round(self.expected_joint, 2),
            "lift": round(self.lift, 2),
            "p_value": round(self.p_value, 4),
            "p_adjusted": round(self.p_adjusted, 4),
            "strata": self.strata,
            "sites_offered": list(self.sites_offered),
            "sites_joint": list(self.sites_joint),
            "groups_offered": dict(self.groups_offered),
            "groups_joint": dict(self.groups_joint),
            "not_offered": self.not_offered,
            "excluded_pending": self.excluded_pending,
            "bundle_default_joint": self.bundle_default_joint,
            "eligibility_conflicts": self.eligibility_conflicts,
            "ineligible_joint": self.ineligible_joint,
            "saturated": self.saturated,
            "classification": self.classification,
            "fragile": self.fragile,
            "sensitivities": [item.as_dict() for item in self.sensitivities],
            "warnings": list(self.warnings),
        }


def analyze_pairs(
    observations: Sequence[Observation],
    catalog: Catalog,
    *,
    scope: str | None = None,
    config: EvidenceConfig | None = None,
    as_of: str | None = None,
) -> list[PairEvidence]:
    return analyze_scopes(observations, catalog, scopes=[scope], config=config, as_of=as_of)[
        scope or "region"
    ]


def analyze_scopes(
    observations: Sequence[Observation],
    catalog: Catalog,
    *,
    scopes: Sequence[str | None],
    config: EvidenceConfig | None = None,
    as_of: str | None = None,
) -> dict[str, list[PairEvidence]]:
    """把全地区与各站点放进同一族做多重比较校正。

    同一批候选组合在不同范围重复检验，若每个范围各自校正，会系统性高估
    证据；因此先汇总所有范围的结果，再统一做 Holm。
    """
    config = config or EvidenceConfig()
    as_of = as_of or _reference_day(observations)
    codes = sorted(code for code in catalog.codes() if _active_on(catalog, code, as_of))

    by_scope: dict[str, list[PairEvidence]] = {}
    units_by_scope: dict[str, list[HouseholdObservation]] = {}
    for scope in scopes:
        units = dedupe_by_household(
            observations,
            site=scope,
            include_shared=False,
            include_automatic=config.include_automatic,
        )
        units_by_scope[scope or "region"] = units
        results: list[PairEvidence] = []
        for index, a in enumerate(codes):
            for b in codes[index + 1 :]:
                evidence = _pair(a, b, units, catalog, scope, config)
                if evidence.applicable:
                    results.append(evidence)
        by_scope[scope or "region"] = results

    _holm([evidence for items in by_scope.values() for evidence in items])
    for scope_key, items in by_scope.items():
        items.sort(key=lambda item: (item.p_value, item.a, item.b))
        scope = None if scope_key == "region" else scope_key
        units = units_by_scope[scope_key]
        for rank, evidence in enumerate(items, start=1):
            evidence.rank = rank
            evidence.classification = _classify(evidence, config)
            if config.robustness:
                evidence.sensitivities = _sensitivities(
                    evidence, units, catalog, scope, config
                )
            evidence.warnings = tuple(
                _pair_warnings(evidence, catalog, units, scope)
            )
    return by_scope


def _pair(
    a: str,
    b: str,
    units: Sequence[HouseholdObservation],
    catalog: Catalog,
    scope: str | None,
    config: EvidenceConfig,
) -> PairEvidence:
    applicable: list[HouseholdObservation] = []
    not_offered = 0
    excluded_pending = 0
    for unit in units:
        observation = unit.observation
        if observation.pending_candidates & {a, b}:
            excluded_pending += 1
            continue
        eligible = not config.require_eligibility or (
            catalog.eligible(a, unit.household, observation.day)
            and catalog.eligible(b, unit.household, observation.day)
        )
        shown = a in observation.displayed and b in observation.displayed
        if eligible and shown:
            applicable.append(unit)
        elif eligible and not shown:
            not_offered += 1

    base = _counts(applicable, a, b, scope, config, catalog)
    eligibility_conflicts = sum(
        1
        for unit in applicable
        if catalog.eligibility_conflict(a, unit.household, unit.observation.day)
        or catalog.eligibility_conflict(b, unit.household, unit.observation.day)
    )
    ineligible_joint = sum(
        1
        for unit in applicable
        if a in unit.chosen
        and b in unit.chosen
        and not (
            catalog.eligible(a, unit.household, unit.observation.day)
            and catalog.eligible(b, unit.household, unit.observation.day)
        )
    )
    saturated = sum(
        1
        for unit in applicable
        if unit.observation.chosen and unit.observation.chosen >= unit.observation.displayed
    )
    period = ""
    if config.period:
        periods = {unit.observation.day[:7] for unit in applicable}
        period = ",".join(sorted(periods)) if len(periods) == 1 else "多期间"

    return PairEvidence(
        a=a,
        b=b,
        scope=scope or "region",
        period=period,
        applicable=base["applicable"],
        households=base["households"],
        joint=base["joint"],
        joint_households=base["joint_households"],
        only_a=base["only_a"],
        only_b=base["only_b"],
        neither=base["neither"],
        expected_joint=base["expected_joint"],
        p_value=base["p_value"],
        p_adjusted=base["p_value"],
        strata=base["strata"],
        sites_offered=base["sites_offered"],
        sites_joint=base["sites_joint"],
        groups_offered=base["groups_offered"],
        groups_joint=base["groups_joint"],
        not_offered=not_offered,
        excluded_pending=excluded_pending,
        bundle_default_joint=base["bundle_default_joint"],
        eligibility_conflicts=eligibility_conflicts,
        ineligible_joint=ineligible_joint,
        saturated=saturated,
    )


def _counts(
    applicable: Sequence[HouseholdObservation],
    a: str,
    b: str,
    scope: str | None,
    config: EvidenceConfig,
    catalog: Catalog,
) -> dict[str, object]:
    strata: dict[tuple[str, ...], list[tuple[bool, bool]]] = defaultdict(list)
    for unit in applicable:
        selected = unit.observation.selected(config.include_automatic)
        strata[config.stratum_key(unit.observation, scope)].append((a in selected, b in selected))

    joint = sum(1 for unit in applicable if a in unit.chosen and b in unit.chosen)
    only_a = sum(1 for unit in applicable if a in unit.chosen and b not in unit.chosen)
    only_b = sum(1 for unit in applicable if b in unit.chosen and a not in unit.chosen)
    neither = len(applicable) - joint - only_a - only_b
    expected = sum(
        (sum(1 for left, _ in rows if left) * sum(1 for _, right in rows if right)) / len(rows)
        for rows in strata.values()
        if rows
    )
    groups_offered = Counter(catalog.group_of(unit.household) for unit in applicable)
    groups_joint = Counter(
        catalog.group_of(unit.household)
        for unit in applicable
        if a in unit.chosen and b in unit.chosen
    )
    return {
        "applicable": len(applicable),
        "households": len({unit.household for unit in applicable}),
        "joint": joint,
        "joint_households": len(
            {unit.household for unit in applicable if a in unit.chosen and b in unit.chosen}
        ),
        "only_a": only_a,
        "only_b": only_b,
        "neither": neither,
        "expected_joint": expected,
        "p_value": _stratified_overlap_p(strata),
        "strata": len(strata),
        "sites_offered": tuple(sorted({unit.site for unit in applicable})),
        "sites_joint": tuple(
            sorted({unit.site for unit in applicable if a in unit.chosen and b in unit.chosen})
        ),
        "groups_offered": dict(groups_offered),
        "groups_joint": dict(groups_joint),
        "bundle_default_joint": sum(
            1
            for unit in applicable
            if a in unit.chosen
            and b in unit.chosen
            and (unit.observation.automatic & {a, b})
        ),
    }


def _stratified_overlap_p(strata: Mapping[tuple[str, ...], Sequence[tuple[bool, bool]]]) -> float:
    """条件独立下的超几何卷积，返回 P(总重叠 >= 观察值)。"""
    distribution: dict[int, int] = {0: 1}
    observed = 0
    for rows in strata.values():
        if not rows:
            continue
        left = [row[0] for row in rows]
        right = [row[1] for row in rows]
        n = len(left)
        x = sum(1 for value in left if value)
        y = sum(1 for value in right if value)
        lower = max(0, x + y - n)
        upper = min(x, y)
        new: dict[int, int] = defaultdict(int)
        for total, ways in distribution.items():
            for overlap in range(lower, upper + 1):
                new[total + overlap] += ways * comb(x, overlap) * comb(n - x, y - overlap)
        distribution = dict(new)
        observed += sum(1 for left_value, right_value in rows if left_value and right_value)
    total = sum(distribution.values())
    if not total:
        return 1.0
    return sum(ways for overlap, ways in distribution.items() if overlap >= observed) / total


def _holm(results: Sequence[PairEvidence]) -> None:
    ordered = sorted(results, key=lambda item: item.p_value)
    family = len(ordered)
    running = 0.0
    for rank, evidence in enumerate(ordered, start=1):
        adjusted = min(1.0, evidence.p_value * (family - rank + 1))
        running = max(running, adjusted)
        evidence.p_adjusted = running


def _classify(evidence: PairEvidence, config: EvidenceConfig) -> str:
    if (
        evidence.applicable < config.min_applicable
        or evidence.joint_households < config.min_joint_households
        or len(evidence.sites_joint) < config.min_sites
    ):
        return INSUFFICIENT
    if evidence.p_adjusted <= config.alpha and evidence.joint > evidence.expected_joint:
        return SUPPORTED
    if evidence.p_value <= config.alpha and evidence.joint > evidence.expected_joint:
        return NOMINAL
    applicable_rate = evidence.joint / evidence.applicable if evidence.applicable else 0.0
    if applicable_rate >= 0.5:
        return CO_OCCURRENCE_ONLY
    return NO_EVIDENCE


def _sensitivities(
    evidence: PairEvidence,
    units: Sequence[HouseholdObservation],
    catalog: Catalog,
    scope: str | None,
    config: EvidenceConfig,
) -> tuple[Sensitivity, ...]:
    results: list[Sensitivity] = []
    pair = {evidence.a, evidence.b}

    eligible_units = [
        unit
        for unit in units
        if evidence.a in unit.observation.displayed
        and evidence.b in unit.observation.displayed
        and not (unit.observation.pending_candidates & pair)
        and catalog.eligible(evidence.a, unit.household, unit.observation.day)
        and catalog.eligible(evidence.b, unit.household, unit.observation.day)
    ]
    results.append(_sensitivity("仅计当日资格合格家庭", eligible_units, evidence, scope, config, catalog))

    non_saturated = [
        unit
        for unit in units
        if not (
            unit.observation.chosen
            and unit.observation.chosen >= unit.observation.displayed
        )
    ]
    results.append(_sensitivity("剔除全选篮子", non_saturated, evidence, scope, config, catalog))

    if not config.include_automatic:
        automatic_config = EvidenceConfig(
            alpha=config.alpha,
            min_applicable=config.min_applicable,
            min_joint_households=config.min_joint_households,
            min_sites=config.min_sites,
            include_automatic=True,
            require_eligibility=config.require_eligibility,
            period=config.period,
            robustness=False,
        )
        results.append(
            _sensitivity(
                "并入套餐默认项", list(units), evidence, scope, automatic_config, catalog
            )
        )
    return tuple(results)


def _sensitivity(
    label: str,
    units: Sequence[HouseholdObservation],
    evidence: PairEvidence,
    scope: str | None,
    config: EvidenceConfig,
    catalog: Catalog,
) -> Sensitivity:
    pair = {evidence.a, evidence.b}
    applicable = [
        unit
        for unit in units
        if evidence.a in unit.observation.displayed
        and evidence.b in unit.observation.displayed
        and not (unit.observation.pending_candidates & pair)
    ]
    counts = _counts(applicable, evidence.a, evidence.b, scope, config, catalog)
    proxy = PairEvidence(
        a=evidence.a,
        b=evidence.b,
        scope=scope or "region",
        period="",
        applicable=counts["applicable"],
        households=counts["households"],
        joint=counts["joint"],
        joint_households=counts["joint_households"],
        only_a=counts["only_a"],
        only_b=counts["only_b"],
        neither=counts["neither"],
        expected_joint=counts["expected_joint"],
        p_value=counts["p_value"],
        p_adjusted=counts["p_value"],
        strata=counts["strata"],
        sites_offered=counts["sites_offered"],
        sites_joint=counts["sites_joint"],
        groups_offered=counts["groups_offered"],
        groups_joint=counts["groups_joint"],
        not_offered=0,
        excluded_pending=0,
        bundle_default_joint=counts["bundle_default_joint"],
        eligibility_conflicts=0,
        ineligible_joint=0,
        saturated=0,
    )
    return Sensitivity(
        label=label,
        applicable=counts["applicable"],
        joint=counts["joint"],
        p_value=counts["p_value"],
        classification=_classify(proxy, config),
    )


def _pair_warnings(
    evidence: PairEvidence,
    catalog: Catalog,
    units: Sequence[HouseholdObservation],
    scope: str | None,
) -> list[str]:
    warnings: list[str] = []
    if evidence.classification == INSUFFICIENT:
        warnings.append(
            f"样本不足：适用 {evidence.applicable} 户、共同选择 {evidence.joint_households} 户，"
            "不足以放入柜台建议"
        )
    if evidence.classification == NOMINAL:
        warnings.append(
            "单个组合名义检验显著，但同族多个候选组合复核后不再显著；"
            "只能作为待验证线索，不能据此调整目录"
        )
    if evidence.classification == CO_OCCURRENCE_ONLY:
        warnings.append(
            "只是常同时出现；在相同备选集合下没有超出独立预期的共同选择，"
            "可能反映固定套餐或流行度，不宜据此调整目录"
        )
    if evidence.classification in {SUPPORTED, NOMINAL}:
        if evidence.ineligible_joint:
            warnings.append(
                f"共同选择中有 {evidence.ineligible_joint} 户按当日目录不具备办理资格，"
                "证据不能直接当作可办理组合"
            )
        if evidence.fragile:
            changed = [
                item
                for item in evidence.sensitivities
                if item.classification != evidence.classification
            ]
            detail = "；".join(
                f"{item.label}后为 {item.classification}（n={item.applicable}，共同 {item.joint}）"
                for item in changed
            )
            warnings.append("结论对分析口径敏感：" + detail)
        if len(evidence.sites_joint) < len(evidence.sites_offered):
            warnings.append(
                "共同选择只在 "
                + ",".join(evidence.sites_joint)
                + " 有记录，推荐范围应限于这些站点，不能扩到全地区"
            )
        for code in (evidence.a, evidence.b):
            if len(catalog.versions(code)) > 1:
                warnings.append(f"{code} 在样本期间有目录换版，旧推荐范围不能自动继承")
        if evidence.bundle_default_joint:
            warnings.append(
                f"有 {evidence.bundle_default_joint} 户的同时选择来自套餐默认带出，"
                "应与居民另选分开"
            )
        if evidence.excluded_pending:
            warnings.append(
                f"另有 {evidence.excluded_pending} 户因映射待判断未计入，结论可能受影响"
            )
    if evidence.saturated:
        warnings.append(
            f"有 {evidence.saturated} 户选择了当次展示的全部项目，"
            "可能是整单受理而非逐项选择，已单列敏感性检查"
        )
    multi_site = sorted(
        {
            unit.household
            for unit in units
            if len({o.site for o in (unit.observation, *unit.repeats)}) > 1
        }
    )
    if scope is None and multi_site:
        warnings.append(
            f"全地区合并时每户只计一次，涉及 {len(multi_site)} 户跨站重复接洽；"
            "分站证据应以站点分层为准"
        )
    return warnings


def _reference_day(observations: Iterable[Observation]) -> str:
    days = [observation.day for observation in observations]
    return max(days) if days else "9999-12-31"


def _active_on(catalog: Catalog, code: str, day: str) -> bool:
    return catalog.entry_for(code, day) is not None


def pending_mapping_impact(
    observations: Sequence[Observation],
    resolver,
    *,
    scope: str | None = None,
) -> list[dict[str, object]]:
    """映射冲突时保留待判断，并列出可能被影响的记录与组合。"""
    impact: list[dict[str, object]] = []
    for window in resolver.pending_windows():
        candidates = set(window["candidates"])
        affected = [
            observation
            for observation in observations
            if observation.site == window["site"]
            and window["start"] <= observation.day < window["end"]
            and (observation.pending_candidates & candidates)
        ]
        if scope is not None:
            affected = [observation for observation in affected if observation.site == scope]
        if not affected:
            continue
        combos: set[tuple[str, str]] = set()
        for observation in affected:
            known = observation.chosen - candidates
            for pending in sorted(observation.pending_candidates):
                for other in sorted(known | (observation.pending_candidates - {pending})):
                    combos.add(tuple(sorted((pending, other))))
        impact.append(
            {
                "alias": window["alias"],
                "site": window["site"],
                "start": window["start"],
                "end": window["end"],
                "candidates": sorted(candidates),
                "rows": list(window["rows"]),
                "reason": window["reason"],
                "affected_sessions": sorted({observation.session for observation in affected}),
                "affected_households": sorted({observation.household for observation in affected}),
                "possibly_affected_pairs": [f"{left}+{right}" for left, right in sorted(combos)],
            }
        )
    return impact
