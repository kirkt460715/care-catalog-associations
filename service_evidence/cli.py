"""命令行入口：生成证据与目录调整报告。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .counselor import counselor_view, render_markdown
from .data import load_tables
from .evidence import EvidenceConfig
from .paths import DEFAULT_DATA
from .render import render_report
from .report import build_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="交换资料目录")
    parser.add_argument("--today", default="2026-11-22", help="当前日期（目录换版核查基准）")
    parser.add_argument("--no-per-site", action="store_true", help="只输出全地区分析")
    parser.add_argument("--include-automatic", action="store_true", help="把套餐默认项并入选择")
    parser.add_argument("--min-applicable", type=int, default=10)
    parser.add_argument("--min-joint-households", type=int, default=5)
    parser.add_argument("--output", type=Path, help="写入 JSON 文件，默认输出到标准输出")
    parser.add_argument(
        "--format", choices=("json", "markdown", "counselor"), default="json"
    )
    parser.add_argument("--household", help="顾问入口中针对某户生成的说明")
    arguments = parser.parse_args(argv)

    tables = load_tables(arguments.data)
    config = EvidenceConfig(
        include_automatic=arguments.include_automatic,
        min_applicable=arguments.min_applicable,
        min_joint_households=arguments.min_joint_households,
    )
    report = build_report(
        tables,
        today=arguments.today,
        per_site=not arguments.no_per_site,
        config=config,
    )
    if arguments.format == "markdown":
        text = render_report(report)
    elif arguments.format == "counselor":
        view = counselor_view(
            tables, today=arguments.today, household=arguments.household
        )
        text = render_markdown(view)
    else:
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if arguments.output:
        arguments.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="" if text.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
