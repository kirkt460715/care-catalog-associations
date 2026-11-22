"""居民办理说明的保存、复核与影响范围。

三类内容必须分开：
* ``current``：按当下目录可以发给新家庭的说明；
* ``sent``：已经发出的原说明，只读保留，附当时采信的来源与受理状态；
* ``clarify``：真的需要顾问澄清的个案。

已发说明若包含现已停止单办或停办的项目，不把它撤回或覆盖；保留当时受理部分
如何接续，同时对新请求按当下目录处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .aliases import AliasResolver, AliasRow
from .baskets import Observation
from .catalog import Catalog, CombinationCheck
from .data import table

CURRENT = "current"
SENT = "sent"
CLARIFY = "clarify"

# 说明本身没有问题时仍然属于已发记录；下列情况需要顾问澄清。
_AMBIGUOUS_STATES = frozenset({"ambiguous", "pending", "reviewed_correction"})


@dataclass
class AdviceRecord:
    id: str
    household: str
    site: str
    services: tuple[str, ...]
    source: str
    at: str
    accepted: tuple[str, ...]
    source_row: AliasRow | None = None
    alias_rows_in_effect: tuple[str, ...] = ()


@dataclass
class AdviceReview:
    record: AdviceRecord
    bucket: str
    sent_check: CombinationCheck
    current_check: CombinationCheck
    findings: tuple[str, ...]
    continuity: tuple[str, ...]
    affected_aliases: tuple[str, ...]
    source_status: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.record.id,
            "household": self.record.household,
            "site": self.record.site,
            "services": list(self.record.services),
            "source": self.record.source,
            "at": self.record.at,
            "accepted": list(self.record.accepted),
            "bucket": self.bucket,
            "source_status": self.source_status,
            "findings": list(self.findings),
            "continuity": list(self.continuity),
            "affected_aliases": list(self.affected_aliases),
            "sent_check": _check_dict(self.sent_check),
            "current_check": _check_dict(self.current_check),
        }


def _check_dict(check: CombinationCheck) -> dict[str, object]:
    return {
        "codes": list(check.codes),
        "actionable": check.actionable,
        "advisory_only": check.advisory_only,
        "separately_applicable": check.separately_applicable,
        "eligibility_ok": check.eligibility_ok,
        "blockers": list(check.blockers),
        "notes": list(check.notes),
        "order": list(check.order),
        "audience": check.audience,
    }


def load_advice(
    tables: Mapping[str, list[dict[str, str]]], resolver: AliasResolver
) -> list[AdviceRecord]:
    records: list[AdviceRecord] = []
    for row in table(tables, "advice"):
        try:
            source_row = resolver.row(row["source"])
        except KeyError:
            source_row = None
        in_effect: tuple[str, ...] = ()
        if source_row is not None:
            in_effect = tuple(
                alias.id
                for alias in resolver.active_rows(source_row.alias, row["site"], row["at"])
            )
        records.append(
            AdviceRecord(
                id=row["id"],
                household=row["household"],
                site=row["site"],
                services=tuple(filter(None, row["services"].split(","))),
                source=row["source"],
                at=row["at"],
                accepted=tuple(filter(None, row["accepted"].split(","))),
                source_row=source_row,
                alias_rows_in_effect=in_effect,
            )
        )
    return records


def review_sent_advice(
    tables: Mapping[str, list[dict[str, str]]],
    *,
    today: str,
    resolver: AliasResolver | None = None,
    catalog: Catalog | None = None,
) -> list[AdviceReview]:
    resolver = resolver or AliasResolver(tables)
    catalog = catalog or Catalog(tables)
    reviews: list[AdviceReview] = []
    for record in load_advice(tables, resolver):
        findings: list[str] = []
        continuity: list[str] = []
        affected: set[str] = set()
        blocking = False

        sent_check = catalog.check_combination(record.services, record.household, record.at)
        current_check = catalog.check_combination(record.services, record.household, today)

        source_status = "ok"
        if record.source_row is not None:
            status = resolver.resolve(
                record.source_row.alias, record.site, record.at
            ).status
            source_status = status
            if status in _AMBIGUOUS_STATES:
                blocking = True
                findings.append(
                    f"来源 {record.source} 所属用语在该站点与期间仍有待判断的映射，"
                    "需先复核当时采信的是哪一种含义"
                )
            if record.source_row.canonical not in record.services:
                blocking = True
                findings.append(
                    f"来源映射 {record.source} 指向 {record.source_row.canonical}，"
                    f"说明却列出 {','.join(record.services)}，需复核当时采信依据"
                )
            conflicting = _conflicting_rows(record, resolver)
            if conflicting:
                affected.update(conflicting)
                blocking = True
                findings.append(
                    "发出时同站点同期间存在口径不一致的映射：" + ",".join(sorted(conflicting))
                )

        stopped = False
        for code in record.services:
            sent_entry = catalog.entry_for(code, record.at)
            current_entry = catalog.entry_for(code, today)
            if sent_entry is not None and not sent_entry.independent:
                findings.append(f"{code} 在发出当时不能单办，原说明据此作出")
            if current_entry is None:
                findings.append(f"{code} 自目录换版后已停止办理；新请求不能按原说明提交")
                stopped = True
            elif (
                sent_entry is not None
                and sent_entry.independent
                and not current_entry.independent
            ):
                findings.append(
                    f"{code} 自 {current_entry.since} 起停止单办；新请求应按当下目录处理"
                )
                stopped = True
            if not catalog.eligible(code, record.household, today):
                findings.append(
                    f"{code} 当前资格不符（{catalog.group_of(record.household)}）"
                )
                blocking = True

        if any("互斥" in blocker for blocker in sent_check.blockers):
            findings.append("原说明组合含互斥服务，应改为提醒顾问核实，不能作为联合办理建议")

        preconditions = [
            blocker
            for blocker in sent_check.blockers
            if "需先办理" in blocker or "资格不符" in blocker
        ]
        if preconditions:
            blocking = True
            findings.append(
                "原说明在发出时未满足办理条件：" + "；".join(preconditions)
                + "，需澄清当时是否已按顺序或资格另行处理"
            )

        if not sent_check.separately_applicable and sent_check.codes:
            findings.append("原说明未核对能否分开申请，需确认当时的受理方式")

        if not findings and not current_check.actionable and current_check.codes:
            blocking = True
            findings.append(
                "按当下目录，该组合已不能直接办理：" + "；".join(current_check.blockers)
            )

        if record.accepted:
            outside = [code for code in record.accepted if code not in record.services]
            if outside:
                findings.append("受理记录包含说明之外的项目：" + ",".join(outside))
            continuity.extend(
                f"{code} 已于 {record.at} 受理，承办人保留当时的办理路径与完成状态"
                for code in record.accepted
            )
            if stopped:
                continuity.append(
                    "原说明保留为已发记录；已受理部分按当时口径接续，"
                    "新请求按当下目录重新核查，不沿用原说明"
                )
            elif blocking:
                continuity.append("已受理部分维持原状，待映射复核结论明确后再处理后续请求")

        if findings and blocking:
            bucket = CLARIFY
        elif findings and not stopped:
            bucket = CLARIFY
        elif not findings and current_check.actionable:
            bucket = CURRENT
        else:
            bucket = SENT

        reviews.append(
            AdviceReview(
                record=record,
                bucket=bucket,
                sent_check=sent_check,
                current_check=current_check,
                findings=tuple(dict.fromkeys(findings)),
                continuity=tuple(dict.fromkeys(continuity)),
                affected_aliases=tuple(sorted(affected)),
                source_status=source_status,
            )
        )
    return reviews


def _conflicting_rows(record: AdviceRecord, resolver: AliasResolver) -> set[str]:
    if record.source_row is None:
        return set()
    conflicts: set[str] = set()
    for alias_id in record.alias_rows_in_effect:
        row = resolver.row(alias_id)
        if row.canonical != record.source_row.canonical:
            conflicts.add(alias_id)
    return conflicts


def alias_impact(
    alias_id: str,
    tables: Mapping[str, list[dict[str, str]]],
    *,
    resolver: AliasResolver | None = None,
    observations: Sequence[Observation] | None = None,
) -> dict[str, object]:
    """某条映射被重新裁定后，需复核的证据与已发说明的范围。

    只影响同站点同用语；其他站点的同名用语不受牵连。
    """
    resolver = resolver or AliasResolver(tables)
    anchor = resolver.row(alias_id)
    affected_advice = sorted(
        {
            record.id
            for record in load_advice(tables, resolver)
            if record.source_row is not None
            and record.source_row.alias == anchor.alias
            and record.source_row.site == anchor.site
            and anchor.covers(record.at)
        }
    )
    affected_sessions: set[str] = set()
    affected_households: set[str] = set()
    if observations is not None:
        for observation in observations:
            if observation.site != anchor.site or not anchor.covers(observation.day):
                continue
            if not (observation.pending_candidates or observation.pending_codes):
                continue
            if (
                observation.pending_candidates
                and anchor.canonical not in observation.pending_candidates
            ):
                continue
            affected_sessions.add(observation.session)
            affected_households.add(observation.household)

    return {
        "alias_id": alias_id,
        "alias": anchor.alias,
        "site": anchor.site,
        "canonical": anchor.canonical,
        "period": (anchor.start, anchor.end or "持续"),
        "affected_advice": affected_advice,
        "affected_sessions": sorted(affected_sessions),
        "affected_households": sorted(affected_households),
        "unaffected_same_name_sites": sorted(
            {row.site for row in resolver.same_name_other_sites(alias_id) if row.site != anchor.site}
        ),
    }


def current_guidance(
    catalog: Catalog,
    *,
    today: str,
    household: str,
    candidates: Sequence[str],
) -> list[CombinationCheck]:
    """按当下目录生成可给新家庭的说明，并标注互斥时的提醒用途。"""
    checks: list[CombinationCheck] = []
    for code in candidates:
        checks.append(catalog.check_combination([code], household, today))
    if len(candidates) > 1:
        checks.append(catalog.check_combination(list(candidates), household, today))
    return checks
