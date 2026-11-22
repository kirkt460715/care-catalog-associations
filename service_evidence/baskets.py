"""把柜台原始记录整理成“每户一次接洽”的观察。

关键约束
--------
* 一次会话里出现多个家庭时，按家庭拆成不同观察，绝不把不同家庭的需求并成一个组合。
* ``automatic`` 是套餐默认带出的项目，只有 ``chosen`` 才是居民另选；
  两者分开保存，分析时可以单独开关。
* ``retracted`` 等撤回/替换记录不当作已办理；历史只剩套餐总项时也不反推子项。
* 原始用语与 observed_id 冲突、映射待判断时，该条目不进入主分析，
  但保留候选与受影响记录供复核。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .aliases import AliasResolver, Resolution
from .catalog import Catalog
from .data import table

CHOSEN = "chosen"
AUTOMATIC = "automatic"
WITHDRAWN_STATES = frozenset({"retracted", "withdrawn", "replaced", "cancelled"})

SELECTED_STATES = frozenset({CHOSEN})
DEFAULTED_STATES = frozenset({AUTOMATIC})


@dataclass
class LineRecord:
    id: str
    session: str
    household: str
    alias: str
    observed_id: str
    state: str
    parent: str


@dataclass
class Observation:
    session: str
    household: str
    day: str
    site: str
    channel: str
    displayed: frozenset[str]
    chosen: frozenset[str] = frozenset()
    automatic: frozenset[str] = frozenset()
    withdrawn: frozenset[str] = frozenset()
    pending_codes: frozenset[str] = frozenset()
    pending_candidates: frozenset[str] = frozenset()
    mapping_rows: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    eligibility_conflicts: tuple[str, ...] = ()
    defaulted_by_parent: Mapping[str, frozenset[str]] = field(default_factory=dict)
    unexpanded_parents: frozenset[str] = frozenset()
    line_ids: tuple[str, ...] = ()
    own: bool = True

    @property
    def shared(self) -> bool:
        return not self.own

    @property
    def pending(self) -> bool:
        return bool(self.pending_codes or self.pending_candidates)

    @property
    def usable(self) -> bool:
        """能否进入主分析：映射已定、且不是其他家庭借本会话办理。"""
        return self.own and not self.pending

    @property
    def basket(self) -> frozenset[str]:
        return self.chosen

    def selected(self, include_automatic: bool = False) -> frozenset[str]:
        if include_automatic:
            return frozenset(self.chosen | self.automatic)
        return self.chosen


@dataclass
class HouseholdObservation:
    """去重后每户一个观察，重复接洽只作记录，不重复计入购买次数。"""

    household: str
    observation: Observation
    repeats: tuple[Observation, ...] = ()
    repeated_sites: tuple[str, ...] = ()

    @property
    def day(self) -> str:
        return self.observation.day

    @property
    def site(self) -> str:
        return self.observation.site

    @property
    def channel(self) -> str:
        return self.observation.channel

    @property
    def displayed(self) -> frozenset[str]:
        return self.observation.displayed

    @property
    def chosen(self) -> frozenset[str]:
        return self.observation.chosen


def build_observations(
    tables: Mapping[str, list[dict[str, str]]],
    *,
    resolver: AliasResolver | None = None,
    catalog: Catalog | None = None,
) -> list[Observation]:
    resolver = resolver or AliasResolver(tables)
    catalog = catalog or Catalog(tables)
    sessions = table(tables, "sessions")
    lines = [LineRecord(**{key: row[key] for key in LineRecord.__dataclass_fields__}) for row in table(tables, "line_items")]
    known_parents = frozenset(line.parent for line in lines if line.parent)

    by_session: dict[str, list[LineRecord]] = {}
    for line in lines:
        by_session.setdefault(line.session, []).append(line)

    observations: list[Observation] = []
    for session in sessions:
        session_id = session["session"]
        session_lines = by_session.get(session_id, [])
        households = sorted({line.household for line in session_lines})
        if session["household"] not in households:
            households.insert(0, session["household"])
        for household in households:
            household_lines = [line for line in session_lines if line.household == household]
            observations.append(
                _build_one(
                    session=session,
                    household=household,
                    lines=household_lines,
                    own=household == session["household"],
                    resolver=resolver,
                    catalog=catalog,
                    known_parents=known_parents,
                )
            )
    return observations


def _build_one(
    *,
    session: Mapping[str, str],
    household: str,
    lines: Sequence[LineRecord],
    own: bool,
    resolver: AliasResolver,
    catalog: Catalog,
    known_parents: frozenset[str],
) -> Observation:
    site = session["site"]
    day = session["day"]
    displayed = frozenset(filter(None, session["displayed"].split(",")))
    chosen: set[str] = set()
    automatic: set[str] = set()
    withdrawn: set[str] = set()
    pending_codes: set[str] = set()
    pending_candidates: set[str] = set()
    mapping_rows: list[str] = []
    issues: list[str] = []
    eligibility: list[str] = []
    defaulted: dict[str, set[str]] = {}
    line_ids: list[str] = []

    for line in lines:
        line_ids.append(line.id)
        code, resolution = _read_code(line, site, day, resolver, catalog)
        if resolution is not None:
            mapping_rows.extend(resolution.rows)
            if resolution.status != "resolved":
                pending_candidates.update(resolution.candidates)
                if code is None:
                    pending_codes.add(line.observed_id or line.alias)
                issues.append(
                    f"记录 {line.id}：用语“{line.alias}”在 {site} {day} {resolution.status}（{resolution.reason}）"
                )

        if line.state in WITHDRAWN_STATES:
            if code:
                withdrawn.add(code)
            continue
        if code is None:
            if line.state in SELECTED_STATES:
                pending_codes.add(line.observed_id or line.alias)
            continue

        entry = catalog.entry_for(code, day)
        if entry is None:
            issues.append(f"记录 {line.id}：{code} 在 {day} 不在目录有效期，需复核")
            eligibility.append(code)
        elif not catalog.eligible(code, household, day):
            eligibility.append(code)
            issues.append(f"记录 {line.id}：{household}（{catalog.group_of(household)}）不具备 {code} 资格")
        if code not in displayed:
            issues.append(f"记录 {line.id}：{code} 不在当次展示清单，需复核")

        if line.state in DEFAULTED_STATES:
            automatic.add(code)
            parent = line.parent or ""
            if parent:
                defaulted.setdefault(parent, set()).add(code)
            continue
        if line.state in SELECTED_STATES:
            if code in chosen:
                issues.append(f"记录 {line.id}：{code} 重复登记，按一次计入")
            chosen.add(code)
            continue
        issues.append(f"记录 {line.id}：未知确认状态 {line.state}，不进入分析")
        pending_codes.add(code)

    unexpanded = _unexpanded_parents(chosen, defaulted, known_parents)
    # 历史只剩套餐总项：保留总项本身，绝不把子项当作当时都办过。
    for parent in unexpanded:
        issues.append(f"套餐 {parent} 只登记了总项，未反推子项")

    return Observation(
        session=session["session"],
        household=household,
        day=day,
        site=site,
        channel=session["channel"],
        displayed=displayed,
        chosen=frozenset(chosen),
        automatic=frozenset(automatic),
        withdrawn=frozenset(withdrawn),
        pending_codes=frozenset(pending_codes),
        pending_candidates=frozenset(pending_candidates),
        mapping_rows=tuple(dict.fromkeys(mapping_rows)),
        issues=tuple(dict.fromkeys(issues)),
        eligibility_conflicts=tuple(dict.fromkeys(eligibility)),
        defaulted_by_parent={key: frozenset(value) for key, value in defaulted.items()},
        unexpanded_parents=frozenset(unexpanded),
        line_ids=tuple(line_ids),
        own=own,
    )


def _read_code(
    line: LineRecord,
    site: str,
    day: str,
    resolver: AliasResolver,
    catalog: Catalog,
) -> tuple[str | None, Resolution | None]:
    if line.alias in catalog.codes():
        if line.observed_id and line.observed_id != line.alias:
            return None, Resolution(
                "mismatch",
                None,
                frozenset({line.alias, line.observed_id}),
                (),
                observed_mismatch=True,
                reason="原始用语与 observed_id 冲突",
            )
        return line.alias, None
    resolution = resolver.resolve(line.alias, site, day, line.observed_id)
    if resolution.status == "resolved" and resolution.canonical:
        return resolution.canonical, resolution
    return None, resolution


def _unexpanded_parents(
    chosen: Iterable[str],
    defaulted: Mapping[str, set[str]],
    known_parents: frozenset[str],
) -> set[str]:
    """只对确实作为套餐父项出现、且没有子项记录的总项标记。

    父项集合从原始记录的 ``parent`` 推出；不推断具体缺了哪些子项，
    也不把它们计入购买。
    """
    return {parent for parent in chosen if parent in known_parents and parent not in defaulted}


def dedupe_by_household(
    observations: Sequence[Observation],
    *,
    site: str | None = None,
    include_shared: bool = False,
    include_automatic: bool = False,
    residence_sites: Mapping[str, str] | None = None,
) -> list[HouseholdObservation]:
    """一户一观察，避免重复接洽或合并单据抬高同一组合的购买次数。

    选择顺序：本户自己的接洽优先，其次日期早、会话号小的记录。
    ``repeats`` 保留其余接洽，可用于检查时间变更或合并单据。
    """
    if residence_sites is None:
        residence_sites = _residence_sites(observations)
    candidates = [
        observation
        for observation in observations
        if (include_shared or observation.own)
        and (site is None or observation.site == site)
    ]
    by_household: dict[str, list[Observation]] = {}
    for observation in candidates:
        by_household.setdefault(observation.household, []).append(observation)

    deduped: list[HouseholdObservation] = []
    for household, group in sorted(by_household.items()):
        home = residence_sites.get(household)
        # 同一户多次接洽只取一次：本人、常驻站点、非待判断、较早的记录优先。
        ordered = sorted(
            group,
            key=lambda item: (
                not item.own,
                item.site != home,
                item.pending,
                item.day,
                item.session,
            ),
        )
        primary = ordered[0]
        repeats = tuple(ordered[1:])
        sites = tuple(sorted({item.site for item in repeats} - {primary.site}))
        deduped.append(
            HouseholdObservation(
                household=household,
                observation=primary,
                repeats=repeats,
                repeated_sites=sites,
            )
        )
    return deduped


def _residence_sites(observations: Sequence[Observation]) -> dict[str, str]:
    """每户的常驻站点：取本人接洽里出现最多的站点，平局取最早。"""
    tally: dict[str, Counter[str]] = {}
    first: dict[tuple[str, str], str] = {}
    for observation in observations:
        if not observation.own:
            continue
        tally.setdefault(observation.household, Counter())[observation.site] += 1
        key = (observation.household, observation.site)
        first[key] = min(first.get(key, observation.day), observation.day)
    sites: dict[str, str] = {}
    for household, counter in tally.items():
        best = sorted(counter.items(), key=lambda item: (-item[1], first[(household, item[0])]))
        sites[household] = best[0][0]
    return sites
