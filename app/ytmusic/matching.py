"""歌曲名与歌手相似度打分器（移植自 Xeltra233/youtube-music-api/internal/matching/matching.go）

YouTube Music 搜索接口永不返回空结果（即使输入乱码也会返回 20 条无关结果），
因此通过 NFKC 归一化 + CJK 单字分词 + 双行 Levenshtein 距离计算 0~1 的匹配分。
"""

from __future__ import annotations

import unicodedata

FUZZY_TOKEN_MIN_LEN = 4
FUZZY_TOKEN_THRESHOLD = 0.7
MAX_SCORE_RUNES = 256


def build_display_name(title: str, artists: list[str] | None = None) -> str:
    """生成标准显示名称：'标题 - 歌手1, 歌手2'"""
    title = (title or "").strip()
    named = [a.strip() for a in (artists or []) if a and a.strip()]
    if not named:
        return title
    joined = ", ".join(named)
    if not title:
        return joined
    return f"{title} - {joined}"


def _is_separator(ch: str) -> bool:
    cat = unicodedata.category(ch)
    return cat.startswith("P") or cat.startswith("S")


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        (0x3040 <= cp <= 0x30FF)  # 平假名 / 片假名
        or (0x3400 <= cp <= 0x4DBF)  # CJK 扩展 A
        or (0x4E00 <= cp <= 0x9FFF)  # CJK 基本区
        or (0xF900 <= cp <= 0xFAFF)  # CJK 兼容表意文字
        or (0xAC00 <= cp <= 0xD7AF)  # 韩文音节
        or (0x20000 <= cp <= 0x2FA1F)  # CJK 扩展 B~F
    )


def normalize(s: str) -> str:
    """NFKC 归一化 → 小写 → 标点符号转空格 → 压缩连续空白"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    out: list[str] = []
    pending_space = False
    wrote = False

    for ch in s:
        if ch.isspace() or _is_separator(ch):
            pending_space = wrote
        elif ch.isalpha() or ch.isdigit():
            if pending_space:
                out.append(" ")
                pending_space = False
            out.append(ch.lower())
            wrote = True
        else:
            pending_space = wrote

    return "".join(out)


def tokenize(normalized: str) -> list[str]:
    """将已归一化的字符串切分为词元：拉丁/数字按空白切分，CJK 按单字切分"""
    if not normalized:
        return []
    tokens: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            tokens.append("".join(buf))
            buf.clear()

    for ch in normalized:
        if ch == " ":
            flush()
        elif _is_cjk(ch):
            flush()
            tokens.append(ch)
        else:
            buf.append(ch)
    flush()
    return tokens


def levenshtein(a: str, b: str) -> int:
    """双行滚动数组计算 Levenshtein 编辑距离：O(len(a)*len(b)) 时间，O(min(len)) 空间"""
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(b) > len(a):
        a, b = b, a

    lb = len(b)
    prev = list(range(lb + 1))
    curr = [0] * (lb + 1)

    for i, ca in enumerate(a, 1):
        curr[0] = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(
                prev[j] + 1,      # delete
                curr[j - 1] + 1,  # insert
                prev[j - 1] + cost,  # substitute
            )
        prev, curr = curr, prev
    return prev[lb]


def similarity(a: str, b: str) -> float:
    longest = max(len(a), len(b))
    if longest == 0:
        return 0.0
    dist = levenshtein(a, b)
    if dist >= longest:
        return 0.0
    return 1.0 - (dist / longest)


def _similarity_upper_bound(la: int, lb: int) -> float:
    if la == 0 or lb == 0:
        return 0.0
    return min(la, lb) / max(la, lb)


def _token_score(nq: str, nt: str) -> float:
    query_tokens = tokenize(nq)
    target_tokens = tokenize(nt)
    if not query_tokens or not target_tokens:
        return 0.0

    remaining: dict[str, int] = {}
    for t in target_tokens:
        remaining[t] = remaining.get(t, 0) + 1

    weight = 0.0
    consumed = 0
    unmatched: list[str] = []

    for q in query_tokens:
        if remaining.get(q, 0) > 0:
            remaining[q] -= 1
            weight += 1.0
            consumed += 1
        elif len(q) >= FUZZY_TOKEN_MIN_LEN:
            unmatched.append(q)

    for q in unmatched:
        best_token = ""
        best_sim = 0.0
        for t in target_tokens:
            if remaining.get(t, 0) <= 0 or len(t) < FUZZY_TOKEN_MIN_LEN:
                continue
            upper = _similarity_upper_bound(len(q), len(t))
            if upper < FUZZY_TOKEN_THRESHOLD or upper <= best_sim:
                continue
            sim = similarity(q, t)
            if sim > best_sim:
                best_sim = sim
                best_token = t
        if best_token and best_sim >= FUZZY_TOKEN_THRESHOLD:
            remaining[best_token] -= 1
            weight += best_sim
            consumed += 1

    if consumed == 0:
        return 0.0

    query_coverage = weight / len(query_tokens)
    target_coverage = consumed / len(target_tokens)
    return query_coverage * (0.85 + 0.15 * target_coverage)


def match_score(query: str, target: str) -> float:
    """计算查询词与候选歌曲名（display_name）的综合匹配度（0.0 ~ 1.0）"""
    nq = normalize(query)
    nt = normalize(target)
    if not nq or not nt:
        return 0.0
    if nq == nt:
        return 1.0

    nq = nq[:MAX_SCORE_RUNES]
    nt = nt[:MAX_SCORE_RUNES]

    best = similarity(nq, nt)
    if nq in nt:
        ratio = len(nq) / len(nt)
        sub_score = 0.75 + 0.25 * ratio
        if sub_score > best:
            best = sub_score

    tok_score = _token_score(nq, nt)
    if tok_score > best:
        best = tok_score
    return round(best, 4)
