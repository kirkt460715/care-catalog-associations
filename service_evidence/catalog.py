"""服务目录换版与组合可办理性核查。

目录按生效日期保留多版；分析某一历史日期时只能使用该日有效的版本，
不能把换版后的规则倒推回旧记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .data import table


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    code: str
    since: str
    until: str
    eligibility: str
    independent: bool
    excludes: tuple[str, ...]
    requires: tuple[str, ...]

    @classmethod
    def from_row(cls, row: Mapping[str, str]) -> "CatalogEntry":
        return cls(
            id=row["id"],
            code=row["code"],
            since=row["since"],
            until=row["until"],
            eligibility=row["eligibility"],
            independent=row["independent"] == "yes",
            excludes=tuple(filter(None, row["excludes"].split(","))),
            requires=tuple(filter(None, row["requires"].split(","))),
        )

    def covers(self, day: str) -> bool:
        return self.since <= day and (not self.until or day < self.until)


@dataclass(frozen=True)
class CombinationCheck:
    """一组服务在某个家庭、某个日期下的可办理性。"""

    codes: tuple[str, ...]
    day: str
    actionable: bool
    advisory_only: bool
    separately_applicable: bool
    eligibility_ok: bool
    blockers: tuple[str, ...]
    notes: tuple[str, ...]
    order: tuple[str, ...]

    @property
    def audience(self) -> str:
        if self.actionable:
            return "可发给新家庭的办理建议"
        if self.advisory_only:
            return "仅供顾问核实，不可作为联合办理建议"
        return "不可办理"


class Catalog:
    def __init__(self, tables: Mapping[str, list[dict[str, str]]]):
        self.entries = [CatalogEntry.from_row(row) for row in table(tables, "catalog")]
        self.groups = {row["household"]: row["group"] for row in table(tables, "population")}

    # -- 版本 ---------------------------------------------------------
    def entry_for(self, code: str, day: str) -> CatalogEntry | None:
        matches = [entry for entry in self.entries if entry.code == code and entry.covers(day)]
        if len(matches) > 1:
            raise ValueError(f"{code} 在 {day} 有多条同时有效的目录记录")
        return matches[0] if matches else None

    def versions(self, code: str) -> list[CatalogEntry]:
        return sorted((entry for entry in self.entries if entry.code == code), key=lambda e: e.since)

    def codes(self) -> set[str]:
        return {entry.code for entry in self.entries}

    # -- 资格 ---------------------------------------------------------
    def group_of(self, household: str) -> str:
        return self.groups.get(household, "unknown")

    def eligible(self, code: str, household: str, day: str) -> bool:
        entry = self.entry_for(code, day)
        if entry is None:
            return False
        if entry.eligibility == "general":
            return True
        return self.group_of(household) == entry.eligibility

    def eligibility_conflict(self, code: str, household: str, day: str) -> bool:
        """按当日目录判断该户是否真的不具备资格。

        目录登记为 support 而家庭为 general 时，仍要区分“当日版本原本
        就限 support”与“换版后才收紧”，只有后者才是需要提交前核对的冲突。
        """
        entry = self.entry_for(code, day)
        if entry is None or entry.eligibility == "general":
            return False
        return self.group_of(household) != entry.eligibility

    def shared_candidates(self, code: str, day: str) -> set[str]:
        """该日期下真正有机会选择此项目的家庭范围。"""
        entry = self.entry_for(code, day)
        if entry is None:
            return set()
        if entry.eligibility == "general":
            return set(self.groups)
        return {household for household, group in self.groups.items() if group == entry.eligibility}

    # -- 组合 ---------------------------------------------------------
    def check_combination(self, codes: Sequence[str], household: str, day: str) -> CombinationCheck:
        unique = tuple(dict.fromkeys(codes))
        blockers: list[str] = []
        notes: list[str] = []
        order: list[str] = []

        active: dict[str, CatalogEntry] = {}
        for code in unique:
            entry = self.entry_for(code, day)
            if entry is None:
                blockers.append(f"{code} 在 {day} 不在目录有效期内")
                continue
            active[code] = entry
            if not self.eligible(code, household, day):
                blockers.append(f"{code} 当前资格不符（需要 {entry.eligibility}）")

        excludes_hit: list[tuple[str, str]] = []
        for code, entry in active.items():
            for other in entry.excludes:
                if other in active:
                    excludes_hit.append(tuple(sorted((code, other))))
        for left, right in sorted(set(excludes_hit)):
            notes.append(f"{left} 与 {right} 互斥，需向顾问核实实际情形")
        if excludes_hit:
            blockers.append("组合内含互斥服务")

        requires_missing: list[str] = []
        for code, entry in active.items():
            for prerequisite in entry.requires:
                if prerequisite not in active:
                    requires_missing.append(f"{code} 需先办理 {prerequisite}")
        if requires_missing:
            blockers.extend(requires_missing)
        else:
            order = self._topological_order(active)

        separately = all(entry.independent for entry in active.values()) and bool(active)
        non_standalone = [code for code, entry in active.items() if not entry.independent]
        if non_standalone:
            notes.append(f"{','.join(non_standalone)} 当前不能单办，只能作为组合的一部分")
            if len(unique) == 1:
                blockers.append(f"{non_standalone[0]} 当前不能单办，不能作为单项建议发出")

        eligibility_ok = all(self.eligible(code, household, day) for code in unique)
        actionable = not blockers and eligibility_ok and bool(active)
        advisory_only = (not actionable) and bool(excludes_hit)

        return CombinationCheck(
            codes=unique,
            day=day,
            actionable=actionable,
            advisory_only=advisory_only,
            separately_applicable=separately,
            eligibility_ok=eligibility_ok,
            blockers=tuple(dict.fromkeys(blockers)),
            notes=tuple(notes),
            order=tuple(order),
        )

    def _topological_order(self, active: Mapping[str, CatalogEntry]) -> list[str]:
        pending = list(active)
        ordered: list[str] = []
        while pending:
            ready = [code for code in pending if not (set(active[code].requires) & set(pending))]
            if not ready:  # 需要环，按字典序保留，交由人工核查
                ready = sorted(pending)
            ordered.extend(sorted(ready))
            pending = [code for code in pending if code not in ready]
        return ordered

    def catalog_scope_notes(self, codes: Sequence[str], day: str) -> list[str]:
        """大类拆分后，各版本覆盖范围不同，提示推荐范围不能自动继承。"""
        notes: list[str] = []
        for code in dict.fromkeys(codes):
            versions = [entry for entry in self.versions(code) if entry.since <= day]
            if len(versions) <= 1:
                continue
            current = self.entry_for(code, day)
            if current is None:
                continue
            previous = versions[-2]
            if previous.independent != current.independent:
                notes.append(
                    f"{code} 自 {current.since} 起单办条件由 "
                    f"{'可单办' if previous.independent else '不可单办'} 改为 "
                    f"{'可单办' if current.independent else '不可单办'}，旧证据范围不能自动沿用"
                )
        return notes


def entries_for(tables: Mapping[str, list[dict[str, str]]], code: str) -> Iterable[CatalogEntry]:
    return (CatalogEntry.from_row(row) for row in table(tables, "catalog") if row["code"] == code)
