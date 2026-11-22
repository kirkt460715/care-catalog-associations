"""人类可读的目录调整报告（Markdown）。"""

from __future__ import annotations

from collections import Counter
from typing import Mapping

from .report import ACTIONABLE, ADVISORY, DEFER


def render_report(report: Mapping[str, object]) -> str:
    summary = report["summary"]
    lines: list[str] = ["# 服务组合证据与目录调整建议", ""]
    lines.append(f"基准日期：{report['today']}")
    lines.append("")
    lines.append("## 概要")
    lines.append("")
    lines.append("| 项目 | 数量 |")
    lines.append("| --- | --- |")
    lines.append(f"| 观察（按接洽） | {summary['observations']} |")
    lines.append(f"| 映射待判断的接洽 | {summary['pending_mapping_observations']} |")
    lines.append(f"| 其他家庭借同一次会话办理 | {summary['shared_household_observations']} |")
    lines.append(f"| 套餐默认带出条目 | {summary['automatic_defaults']} |")
    lines.append(f"| 撤回/替换条目 | {summary['withdrawn_lines']} |")
    lines.append("")

    lines.append("## 柜台建议")
    lines.append("")
    recommendations = report["recommendations"]
    if not recommendations:
        lines.append("_本轮没有可放入柜台建议的组合。_")
    for item in recommendations:
        evidence = item["evidence"]
        label = {
            ACTIONABLE: "可发给新家庭",
            ADVISORY: "仅供提醒核实",
            DEFER: "暂不进入柜台建议",
        }[item["kind"]]
        lines.append(f"### {item['pair']}（{label}）")
        lines.append("")
        lines.append(
            f"- 适用范围：站点 {','.join(item['scope_sites']) or '未限定'}；"
            f"家庭组 {'、'.join(item['audience']) or '无（当前不可办理）'}"
        )
        lines.append(
            f"- 证据：适用 {evidence['applicable']} 户，共同选择 {evidence['joint']} 户，"
            f"独立预期 {evidence['expected_joint']}，p={evidence['p_value']}，"
            f"校正后 p={evidence['p_adjusted']}"
        )
        if evidence["sensitivities"]:
            lines.append("- 敏感性：")
            for sensitivity in evidence["sensitivities"]:
                lines.append(
                    f"  - {sensitivity['label']}：{sensitivity['classification']}"
                    f"（适用 {sensitivity['applicable']}，共同 {sensitivity['joint']}，"
                    f"p={sensitivity['p_value']}）"
                )
        for reason in item["reasons"]:
            lines.append(f"- 说明：{reason}")
        for gap in item["coverage_gaps"]:
            lines.append(f"- 覆盖/适用范围：{gap}")
        if item["advisory_note"]:
            lines.append(f"- 发送方式：{item['advisory_note']}")
        lines.append("")

    lines.append("## 映射复核")
    lines.append("")
    for impact in report["pending_mapping_impact"]:
        lines.append(
            f"- {impact['alias']} @ {impact['site']} "
            f"{impact['start']} 至 {impact['end']}：候选 "
            f"{'、'.join(impact['candidates'])}，涉及记录 "
            f"{'、'.join(impact['affected_sessions']) or '无'}，"
            f"可能影响的组合 {','.join(impact['possibly_affected_pairs']) or '无'}"
        )
    lines.append("")

    lines.append("## 已发说明")
    lines.append("")
    for bucket, title in (
        ("current", "当前可发"),
        ("sent", "已发原说明"),
        ("clarify", "待澄清"),
    ):
        items = report["advice"][bucket]
        lines.append(f"### {title}（{len(items)}）")
        for item in items:
            lines.append(
                f"- {item['id']}（{item['household']}，{item['site']}）："
                f"{'、'.join(item['services'])}；受理 {','.join(item['accepted']) or '无'}"
            )
            for finding in item["findings"]:
                lines.append(f"  - {finding}")
            for note in item["continuity"]:
                lines.append(f"  - 接续：{note}")
        lines.append("")

    lines.append("## 资料质量")
    lines.append("")
    kinds = Counter(issue["kind"] for issue in report["data_issues"])
    for kind, count in sorted(kinds.items()):
        lines.append(f"- {kind}: {count}")
    return "\n".join(lines).rstrip() + "\n"
