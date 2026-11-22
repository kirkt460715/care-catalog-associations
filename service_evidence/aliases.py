"""原始用语的映射复核：按站点、期间和使用方式限定适用范围。

设计要点
--------
* 同名用语在不同站点可以是不同含义，映射只在登记站点生效。E 站的“上门协助”
  指清洁，不影响 W 站同名词指搬扶，也不应一并停用。
* ``reviewed_correction`` 是业务复核后的更正，优先于同期间的 ``approved``；
  被它取代的旧映射记录在 ``superseded``，便于回溯已发说明。
* ``approved`` 与 ``ambiguous`` 并存、或两份 approved 口径不同时，不替业务
  裁决，解析为 ``pending``，保留全部候选，供后续影响分析。
* ``ambiguous`` 单独存在同样视为待判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .data import table

RESOLVED = "resolved"
PENDING = "pending"
UNMAPPED = "unmapped"
MISMATCH = "mismatch"

CORRECTION = "reviewed_correction"
APPROVED = "approved"
AMBIGUOUS = "ambiguous"

STATE_LABELS = {
    RESOLVED: "已按登记口径解析",
    PENDING: "待业务判断",
    UNMAPPED: "无可用映射",
    MISMATCH: "原始用语与代码冲突",
}


@dataclass(frozen=True)
class AliasRow:
    id: str
    alias: str
    site: str
    start: str
    end: str
    canonical: str
    state: str

    @classmethod
    def from_row(cls, row: Mapping[str, str]) -> "AliasRow":
        return cls(
            id=row["id"],
            alias=row["alias"],
            site=row["site"],
            start=row["start"],
            end=row["end"],
            canonical=row["canonical"],
            state=row["state"],
        )

    def covers(self, day: str) -> bool:
        """映射期间为 [start, end)；end 为空表示持续有效。"""
        return self.start <= day and (not self.end or day < self.end)


@dataclass(frozen=True)
class Resolution:
    """一次用语解析的结果。"""

    status: str
    canonical: str | None
    candidates: frozenset[str]
    rows: tuple[str, ...]
    superseded: tuple[str, ...] = ()
    observed_mismatch: bool = False
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.status == RESOLVED

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.status, self.status)


class AliasResolver:
    def __init__(self, tables: Mapping[str, list[dict[str, str]]]):
        self.rows = [AliasRow.from_row(row) for row in table(tables, "aliases")]
        self._by_id = {row.id: row for row in self.rows}
        if len(self._by_id) != len(self.rows):
            raise ValueError("aliases 记录键重复")

    # -- 查询 ---------------------------------------------------------
    def row(self, alias_id: str) -> AliasRow:
        try:
            return self._by_id[alias_id]
        except KeyError as error:
            raise KeyError(f"未知映射 {alias_id}") from error

    def active_rows(self, alias: str, site: str, day: str) -> list[AliasRow]:
        return [
            row for row in self.rows if row.alias == alias and row.site == site and row.covers(day)
        ]

    def same_name_other_sites(self, alias_id: str) -> list[AliasRow]:
        """同名但站点不同，或同站点但期间不重叠的登记。"""
        anchor = self.row(alias_id)
        return [
            row
            for row in self.rows
            if row.alias == anchor.alias
            and row.id != anchor.id
            and (row.site != anchor.site or not _overlaps(row, anchor))
        ]

    def resolve(self, alias: str, site: str, day: str, observed_id: str = "") -> Resolution:
        rows = self.active_rows(alias, site, day)
        if not rows:
            return Resolution(UNMAPPED, None, frozenset(), (), reason="该站点与期间没有登记映射")

        corrections = [row for row in rows if row.state == CORRECTION]
        approved = [row for row in rows if row.state == APPROVED]
        ambiguous = [row for row in rows if row.state == AMBIGUOUS]

        if corrections:
            corrected = {row.canonical for row in corrections}
            if len(corrected) > 1:
                return Resolution(
                    PENDING,
                    None,
                    frozenset(corrected),
                    tuple(row.id for row in corrections),
                    reason="同一期间存在多份口径不同的更正",
                )
            canonical = next(iter(corrected))
            overridden = tuple(
                row.id for row in approved + ambiguous if row.canonical != canonical
            )
            mismatch = bool(observed_id) and observed_id != canonical
            return Resolution(
                MISMATCH if mismatch else RESOLVED,
                canonical,
                frozenset({canonical}),
                tuple(row.id for row in corrections),
                superseded=overridden,
                observed_mismatch=mismatch,
                reason="按业务复核后的更正解读",
            )

        if len({row.canonical for row in approved}) > 1:
            return Resolution(
                PENDING,
                None,
                frozenset({row.canonical for row in approved} | {row.canonical for row in ambiguous}),
                tuple(row.id for row in approved + ambiguous),
                reason="同站点同期间存在两份互相冲突的 approved 映射",
            )
        if ambiguous:
            return Resolution(
                PENDING,
                None,
                frozenset({row.canonical for row in approved} | {row.canonical for row in ambiguous}),
                tuple(row.id for row in rows),
                reason="同期间仍有 ambiguous 登记未复核",
            )
        if not approved:
            return Resolution(UNMAPPED, None, frozenset(), tuple(row.id for row in rows), reason="没有可采信的登记")

        canonical = approved[0].canonical
        mismatch = bool(observed_id) and observed_id != canonical
        return Resolution(
            MISMATCH if mismatch else RESOLVED,
            canonical,
            frozenset({canonical}),
            tuple(row.id for row in approved),
            observed_mismatch=mismatch,
            reason="按该站点登记的 approved 映射解读",
        )

    def resolve_id(self, alias_id: str, observed_id: str = "") -> Resolution:
        row = self.row(alias_id)
        return self.resolve(row.alias, row.site, row.start, observed_id)

    # -- 汇总 ---------------------------------------------------------
    def scope_report(self) -> list[dict[str, object]]:
        """按用语分组，列出站点、期间、状态与用法，供业务复核同名异义。"""
        grouped: dict[str, list[dict[str, str]]] = {}
        for row in self.rows:
            grouped.setdefault(row.alias, []).append(
                {
                    "id": row.id,
                    "site": row.site,
                    "start": row.start,
                    "end": row.end or "持续",
                    "canonical": row.canonical,
                    "state": row.state,
                }
            )
        report: list[dict[str, object]] = []
        for alias, entries in sorted(grouped.items()):
            by_site: dict[str, set[str]] = {}
            for entry in entries:
                by_site.setdefault(entry["site"], set()).add(entry["canonical"])
            conflicts = sorted(site for site, meanings in by_site.items() if len(meanings) > 1)
            report.append(
                {
                    "alias": alias,
                    "entries": entries,
                    "sites_with_multiple_meanings": conflicts,
                    "same_name_different_sites": len({frozenset(v) for v in by_site.values()}) > 1,
                }
            )
        return report

    def pending_windows(self) -> list[dict[str, object]]:
        """枚举所有待判断映射窗口；窗口只覆盖真的处于 pending 的期间。"""
        windows: list[dict[str, object]] = []
        grouped: dict[tuple[str, str], list[AliasRow]] = {}
        for row in self.rows:
            grouped.setdefault((row.alias, row.site), []).append(row)
        for (alias, site), rows in sorted(grouped.items()):
            boundaries = sorted({row.start for row in rows} | {row.end for row in rows if row.end})
            for start, end in zip(boundaries, boundaries[1:]):
                resolution = self.resolve(alias, site, start)
                if resolution.status != PENDING:
                    continue
                windows.append(
                    {
                        "alias": alias,
                        "site": site,
                        "start": start,
                        "end": end,
                        "candidates": sorted(resolution.candidates),
                        "rows": list(resolution.rows),
                        "reason": resolution.reason,
                    }
                )
        return windows


def _overlaps(left: AliasRow, right: AliasRow) -> bool:
    left_end = left.end or "9999-12-31"
    right_end = right.end or "9999-12-31"
    return left.start < right_end and right.start < left_end


def alias_rows(tables: Mapping[str, list[dict[str, str]]]) -> list[AliasRow]:
    return [AliasRow.from_row(row) for row in table(tables, "aliases")]


def aliases_for(
    rows: Sequence[AliasRow], alias: str, site: str, day: str
) -> Iterable[AliasRow]:
    return (row for row in rows if row.alias == alias and row.site == site and row.covers(day))
