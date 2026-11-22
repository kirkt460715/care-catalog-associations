"""读取并校验 data/ 下的交换集合。"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .paths import DEFAULT_DATA


def load_tables(directory: Path | str = DEFAULT_DATA) -> dict[str, list[dict[str, str]]]:
    """返回全部集合并做与交换约定一致的结构检查。"""
    root = Path(directory)
    tables: dict[str, list[dict[str, str]]] = {}
    for path in sorted(root.glob("*.json")):
        tables[path.stem] = _read_collection(path)
    if not tables:
        raise ValueError(f"{root} 下没有可读取的集合")
    return tables


def _read_collection(path: Path) -> list[dict[str, str]]:
    import json

    with path.open(encoding="utf-8") as source:
        content = json.load(source)
    fields = content["fields"]
    key = content["key"]
    rows = content["rows"]
    if key not in fields or len(fields) != len(set(fields)) or not isinstance(rows, list):
        raise ValueError(f"{path.stem} 的字段登记无效")
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.keys() != set(fields):
            raise ValueError(f"{path.stem} 有不符合字段登记的记录")
        if not all(isinstance(value, str) for value in row.values()):
            raise ValueError(f"{path.stem} 的字段值必须是字符串")
        if not row[key] or row[key] in seen:
            raise ValueError(f"{path.stem} 的记录键缺失或重复")
        seen.add(row[key])
    return rows


def table(tables: Mapping[str, list[dict[str, str]]], name: str) -> list[dict[str, str]]:
    try:
        return tables[name]
    except KeyError as error:
        raise KeyError(f"缺少集合 {name}") from error
