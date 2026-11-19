from __future__ import annotations

import re
from dataclasses import dataclass

from .roots import AliasTrie, Root, RootLibrary
from .util import cell_str


@dataclass
class MatchResult:
    standard_en: str
    standard_cn: str
    roots_text: str
    status: str
    candidates: str
    note: str


def _unique_roots(items: list[Root]) -> list[Root]:
    seen: set[tuple[str, str]] = set()
    out: list[Root] = []
    for item in items:
        if item.key in seen:
            continue
        seen.add(item.key)
        out.append(item)
    return out


MAX_CANDIDATES = 12

# 目标字段中文名里常带英文/数字/括号注释（如「主键id」「评价渠道(1:电话,2:微信)」），
# 匹配词根前先剔除这些噪声。中英文括号注释一律忽略（支持嵌套/未闭合）。
_BRACKET_PAIR = re.compile(r"[（(][^（()）]*[）)]")
_UNCLOSED = re.compile(r"[（(]")
_STRAY_BRACKET = re.compile(r"[）)]")
_CN_NOISE = re.compile(r"[A-Za-z0-9_:：,，.。;；、\-/\\\s]+")


def clean_cn(text: str) -> str:
    """去掉中文名里的括号注释（中/英文括号）与字母/数字/标点，仅用于词根匹配。"""
    out = cell_str(text)
    while True:
        stripped = _BRACKET_PAIR.sub("", out)
        if stripped == out:
            break
        out = stripped
    # 未闭合的左括号：截断其后内容（视为注释）
    head = _UNCLOSED.split(out)[0]
    out = head if head else _UNCLOSED.sub("", out)
    out = _STRAY_BRACKET.sub("", out)
    return _CN_NOISE.sub("", out).strip()


def _expand_candidates(segments: list[list[Root]], limit: int = MAX_CANDIDATES) -> list[str]:
    """把每段的多义候选项展开成完整英文名，供人工直接挑选。"""
    names = [""]
    for group in segments:
        uniq = _unique_roots(group)
        names = [f"{prefix}_{root.en_abbr}".strip("_") for prefix in names for root in uniq][:limit]
    return names


def _from_path(segments: list[list[Root]], status: str, note: str = "") -> MatchResult:
    chosen: list[Root] = []
    for group in segments:
        chosen.append(_unique_roots(group)[0])
    en = "_".join(r.en_abbr for r in chosen)
    cn = "".join(r.primary_cn for r in chosen)
    text = " + ".join(f"{r.primary_cn}/{r.en_abbr}" for r in chosen)
    expanded = _expand_candidates(segments)
    alternatives = [name for name in expanded if name != en]
    return MatchResult(
        standard_en=en,
        standard_cn=cn,
        roots_text=text,
        status=status,
        candidates=" | ".join(alternatives),
        note=note,
    )


def _cover_chinese(text: str, trie: AliasTrie) -> tuple[list[list[Root]] | None, bool]:
    n = len(text)
    dp: list[tuple[int, list[tuple[int, list[Root]]]] | None] = [None] * (n + 1)
    dp[0] = (0, [])
    for i in range(n):
        if dp[i] is None:
            continue
        hits = trie.prefixes(text, i)
        if not hits:
            continue
        segs_so_far, path = dp[i]
        for end, roots in hits:
            cand_segs = segs_so_far + 1
            cand_path = path + [(end, roots)]
            prev = dp[end]
            last_len = end - i
            better = prev is None or cand_segs < prev[0]
            if not better and prev is not None and cand_segs == prev[0]:
                prev_last = prev[1][-1][0] - (prev[1][-2][0] if len(prev[1]) > 1 else 0)
                better = last_len > prev_last
            if better:
                dp[end] = (cand_segs, cand_path)
    if dp[n] is None:
        return None, False
    segments: list[list[Root]] = []
    multi = False
    for _, roots in dp[n][1]:
        uniq = _unique_roots(roots)
        segments.append(uniq)
        if len(uniq) > 1:
            multi = True
    return segments, multi


def match_field(
    cn_name: str,
    en_name: str,
    library: RootLibrary,
    history_en: str = "",
    history_cn: str = "",
    trie: AliasTrie | None = None,
) -> MatchResult:
    cn = cell_str(cn_name)
    en = cell_str(en_name).lower()
    if history_en:
        return MatchResult(
            standard_en=history_en,
            standard_cn=history_cn or cn,
            roots_text="历史复用",
            status="已匹配",
            candidates="",
            note="历史复用",
        )

    trie = trie or AliasTrie(library.alias_to_roots)
    if cn:
        probe = clean_cn(cn) or cn
        exact = library.alias_to_roots.get(probe)
        if exact:
            uniq = _unique_roots(exact)
            status = "已匹配" if len(uniq) == 1 else "多候选"
            return _from_path([uniq], status, "完整词条" if len(uniq) == 1 else "完整词条多义")
        covered, multi = _cover_chinese(probe, trie)
        if covered:
            status = "多候选" if multi else "已匹配"
            for group in covered:
                if group[0].en_abbr in library.ambiguous_abbrs:
                    status = "多候选"
            return _from_path(covered, status)
        return MatchResult("", cn, "", "未匹配", "", "中文未命中词根")

    tokens = [t for t in en.split("_") if t]
    if not tokens:
        return MatchResult("", "", "", "未匹配", "", "无中文名且英文名为空")
    segments: list[list[Root]] = []
    multi = False
    for token in tokens:
        hits = library.abbr_to_roots.get(token) or library.en_full_to_roots.get(token)
        if not hits:
            return MatchResult("", cn, "", "未匹配", "", f"英文段未命中:{token}")
        uniq = _unique_roots(hits)
        if len(uniq) > 1 or token in library.ambiguous_abbrs:
            multi = True
        segments.append(uniq)
    status = "多候选" if multi else "低置信"
    return _from_path(segments, status, "无中文名，英文拆分匹配")
