"""顾问工作入口：当前说明、已发说明与待澄清个案三栏分开。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .advice import AdviceReview, current_guidance, review_sent_advice
from .aliases import AliasResolver
from .baskets import Observation
from .catalog import Catalog
from .evidence import analyze_scopes
from .report import ACTIONABLE, ADVISORY, build_report


@dataclass
class CounselorView:
    current: list[dict[str, object]]
    sent: list[dict[str, object]]
    clarify: list[dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return {
            "current": self.current,
            "sent": self.sent,
            "clarify": self.clarify,
        }


def counselor_view(
    tables: Mapping[str, list[dict[str, str]]],
    *,
    today: str,
    household: str | None = None,
) -> CounselorView:
    resolver = AliasResolver(tables)
    catalog = Catalog(tables)
    observations = _observations(tables, resolver, catalog)
    reviews = review_sent_advice(tables, today=today, resolver=resolver, catalog=catalog)
    evidence = analyze_scopes(
        observations,
        catalog,
        scopes=[None] + sorted({observation.site for observation in observations}),
    )
    report = build_report(tables, today=today, per_site=True)

    current: list[dict[str, object]] = []
    clarify: list[dict[str, object]] = []
    if household:
        for record in _advice_records(reviews, household):
            pair = list(record.record.services)
            for check in current_guidance(
                catalog,
                today=today,
                household=household,
                candidates=pair,
            ):
                card = {
                    "household": household,
                    "for_new_household": check.actionable,
                    "codes": list(check.codes),
                    "audience": check.audience,
                    "blockers": list(check.blockers),
                    "notes": list(check.notes),
                    "order": list(check.order),
                }
                if check.actionable:
                    card["instruction"] = "可按当下目录办理；发送前核对资格与办理顺序"
                    current.append(card)
                elif len(check.codes) == 1 and any(
                    "不能单办" in blocker for blocker in check.blockers
                ):
                    # 单项已停止单办时不放进“当前可发”，只保留澄清/组合参考。
                    card["instruction"] = "该项已停止单办，不能单独发给新家庭"
                    clarify.append(card)
                else:
                    card["instruction"] = (
                        "不能直接发给新家庭：" + "；".join(check.blockers)
                    )
                    clarify.append(card)
    current.extend(_recommendation_cards(report["recommendations"]))

    sent = [
        {
            "id": review.record.id,
            "household": review.record.household,
            "site": review.record.site,
            "at": review.record.at,
            "services": list(review.record.services),
            "accepted": list(review.record.accepted),
            "source": review.record.source,
            "read_only": True,
            "findings": list(review.findings),
            "continuity": list(review.continuity),
        }
        for review in reviews
        if review.bucket == "sent"
    ]
    clarify.extend([
        {
            "id": review.record.id,
            "household": review.record.household,
            "site": review.record.site,
            "at": review.record.at,
            "services": list(review.record.services),
            "accepted": list(review.record.accepted),
            "source": review.record.source,
            "source_status": review.source_status,
            "findings": list(review.findings),
            "continuity": list(review.continuity),
        }
        for review in reviews
        if review.bucket == "clarify"
    ])
    return CounselorView(current=current, sent=sent, clarify=clarify)


def _observations(tables, resolver, catalog) -> list[Observation]:
    from .baskets import build_observations

    return build_observations(tables, resolver=resolver, catalog=catalog)


def _advice_records(
    reviews: Sequence[AdviceReview], household: str
) -> list[AdviceReview]:
    return [review for review in reviews if review.record.household == household]


def _recommendation_cards(
    recommendations: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    for item in recommendations:
        pair = str(item["pair"])
        codes = pair.split("+")
        kind = item["kind"]
        if kind == ADVISORY:
            cards.append(
                {
                    "codes": codes,
                    "sites": item["scope_sites"],
                    "for_new_household": False,
                    "instruction": item["advisory_note"],
                    "reasons": item["reasons"],
                    "coverage_gaps": item["coverage_gaps"],
                }
            )
        elif kind == ACTIONABLE:
            check = item["check"] or {}
            cards.append(
                {
                    "codes": codes,
                    "sites": item["scope_sites"],
                    "for_new_household": True,
                    "instruction": "可按当下目录分别或组合办理，发送前核对资格与顺序",
                    "order": check.get("order", []),
                    "separately_applicable": check.get("separately_applicable"),
                    "reasons": item["reasons"],
                    "coverage_gaps": item["coverage_gaps"],
                }
            )
    return cards


def render_markdown(view: CounselorView) -> str:
    lines = ["# 顾问工作入口", "", "## 当前可给新家庭的说明", ""]
    if not view.current:
        lines.append("_暂无可发放的说明，先按个人资格与目录逐项核查。_")
    for card in view.current:
        lines.append(
            f"- {'、'.join(card['codes'])}（站点：{','.join(card.get('sites', [])) or '未限定'}）"
            f"：{card.get('instruction', '')}"
        )
        for reason in card.get("reasons", []):
            lines.append(f"  - {reason}")
        for gap in card.get("coverage_gaps", []):
            lines.append(f"  - 覆盖不足：{gap}")
    lines.extend(["", "## 已发出的原说明（只读保留）", ""])
    if not view.sent:
        lines.append("_无。_")
    for item in view.sent:
        lines.append(
            f"- {item['id']}（{item['household']}，{item['site']}，{item['at']}）："
            f"{'、'.join(item['services'])}；受理：{'、'.join(item['accepted']) or '无'}"
        )
        for finding in item["findings"]:
            lines.append(f"  - {finding}")
        for note in item["continuity"]:
            lines.append(f"  - 接续：{note}")
    lines.extend(["", "## 需要澄清的个案", ""])
    if not view.clarify:
        lines.append("_无。_")
    for item in view.clarify:
        if "id" in item:
            lines.append(
                f"- {item['id']}（{item['household']}，{item['site']}）："
                f"{'、'.join(item['services'])}"
            )
        else:
            lines.append(
                f"- {item['household']}：{'、'.join(item['codes'])}"
                f"（{item['instruction']}）"
            )
        for finding in item.get("findings", []):
            lines.append(f"  - {finding}")
        for note in item.get("continuity", []):
            lines.append(f"  - 接续：{note}")
    return "\n".join(lines) + "\n"
