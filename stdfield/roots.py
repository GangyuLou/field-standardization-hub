from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .util import cell_str, norm_header, split_aliases
from .xlsx import excel_file

VOCAB_SHEETS = {"基础词汇", "类别词汇", "时间修饰词"}


@dataclass(frozen=True)
class Root:
    aliases: tuple[str, ...]
    en_full: str
    en_abbr: str
    sheet: str
    source_row: int

    @property
    def primary_cn(self) -> str:
        return self.aliases[0] if self.aliases else ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.en_abbr.lower(), self.en_full.lower())


@dataclass
class RootLibrary:
    roots: list[Root]
    alias_to_roots: dict[str, list[Root]]
    en_full_to_roots: dict[str, list[Root]]
    abbr_to_roots: dict[str, list[Root]]
    ambiguous_abbrs: dict[str, list[Root]]
    conflicts: list[dict]
    filename: str = ""

    def stats(self) -> dict:
        return {
            "词根条数": len(self.roots),
            "中文别名数": len(self.alias_to_roots),
            "英文全称数": len(self.en_full_to_roots),
            "英文缩写数": len(self.abbr_to_roots),
            "多义缩写数": len(self.ambiguous_abbrs),
        }


def _col_map(columns: Iterable) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for col in columns:
        key = norm_header(col)
        if key in {"中文", "中文别名", "中文名"}:
            mapping["cn"] = col
        elif key in {"英文", "英文全称", "英文名"}:
            mapping["en"] = col
        elif key in {"英文缩写", "缩写"}:
            mapping["abbr"] = col
        elif "日期" in key:
            mapping["date"] = col
        elif "修改人" in key or "新增/修改人" in key:
            mapping["author"] = col
    return mapping


def parse_root_library(file_bytes: bytes, filename: str = "") -> RootLibrary:
    xls = excel_file(file_bytes)
    roots: list[Root] = []
    for sheet in xls.sheet_names:
        if sheet in {"词汇问题"}:
            continue
        if VOCAB_SHEETS and sheet not in VOCAB_SHEETS and len(xls.sheet_names) > 1:
            continue
        df = xls.parse(sheet_name=sheet, dtype=object)
        cols = _col_map(df.columns)
        if "cn" not in cols or "en" not in cols or "abbr" not in cols:
            continue
        for idx, row in df.iterrows():
            aliases = split_aliases(row.get(cols["cn"]))
            en_full = cell_str(row.get(cols["en"]))
            en_abbr = cell_str(row.get(cols["abbr"])).lower().replace(" ", "_")
            if not aliases or not en_abbr:
                continue
            roots.append(
                Root(
                    aliases=tuple(aliases),
                    en_full=en_full,
                    en_abbr=en_abbr,
                    sheet=sheet,
                    source_row=int(idx) + 2,
                )
            )
    if not roots:
        raise ValueError("词根库中未识别到有效词条（需包含：中文别名、英文全称、英文缩写 三列）")
    return build_library(roots, filename=filename)


def build_library(roots: list[Root], filename: str = "") -> RootLibrary:
    alias_to_roots: dict[str, list[Root]] = {}
    en_full_to_roots: dict[str, list[Root]] = {}
    abbr_to_roots: dict[str, list[Root]] = {}
    for root in roots:
        for alias in root.aliases:
            alias_to_roots.setdefault(alias, []).append(root)
        if root.en_full:
            en_full_to_roots.setdefault(root.en_full.lower(), []).append(root)
            compact = root.en_full.lower().replace(" ", "_")
            en_full_to_roots.setdefault(compact, []).append(root)
        abbr_to_roots.setdefault(root.en_abbr, []).append(root)

    ambiguous: dict[str, list[Root]] = {}
    conflicts: list[dict] = []
    for abbr, group in abbr_to_roots.items():
        uniq: dict[tuple[str, str], Root] = {}
        for item in group:
            uniq[item.key] = item
        if len(uniq) > 1:
            unique_list = list(uniq.values())
            ambiguous[abbr] = unique_list
            conflicts.append(
                {
                    "缩写": abbr,
                    "冲突数": len(unique_list),
                    "词根": " / ".join(f"{r.primary_cn}({r.en_abbr})" for r in unique_list),
                }
            )

    def _dedupe(mapping: dict[str, list[Root]]) -> dict[str, list[Root]]:
        out: dict[str, list[Root]] = {}
        for key, group in mapping.items():
            seen: set[tuple[str, str]] = set()
            items: list[Root] = []
            for item in group:
                if item.key in seen:
                    continue
                seen.add(item.key)
                items.append(item)
            out[key] = items
        return out

    return RootLibrary(
        roots=roots,
        alias_to_roots=_dedupe(alias_to_roots),
        en_full_to_roots=_dedupe(en_full_to_roots),
        abbr_to_roots=_dedupe(abbr_to_roots),
        ambiguous_abbrs=ambiguous,
        conflicts=conflicts,
        filename=filename,
    )


class AliasTrie:
    def __init__(self, alias_to_roots: dict[str, list[Root]]):
        self.root: dict = {}
        for alias, items in alias_to_roots.items():
            node = self.root
            for ch in alias:
                node = node.setdefault(ch, {})
            node.setdefault("$", []).extend(items)

    def prefixes(self, text: str, start: int) -> list[tuple[int, list[Root]]]:
        node = self.root
        hits: list[tuple[int, list[Root]]] = []
        for i in range(start, len(text)):
            ch = text[i]
            if ch not in node:
                break
            node = node[ch]
            if "$" in node:
                hits.append((i + 1, node["$"]))
        return hits


DEFAULT_ROOT_LIBRARY = Path(__file__).resolve().parent / "assets" / "root_library.xlsx"
DEFAULT_ROOT_LIBRARY_NAME = "常用词根库v2.2_20220624.xlsx"


def default_root_library_path() -> Path | None:
    """随镜像内置的默认词根库；缺失时返回 None（用户可自行上传）。"""
    return DEFAULT_ROOT_LIBRARY if DEFAULT_ROOT_LIBRARY.is_file() else None


def load_default_root_library(filename: str = DEFAULT_ROOT_LIBRARY_NAME) -> tuple[bytes, str]:
    path = default_root_library_path()
    if path is None:
        raise FileNotFoundError("内置词根库缺失，请手动上传词根库文件")
    return path.read_bytes(), filename
