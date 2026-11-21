"""
Pure-Python O(N) Aho-Corasick Multi-Pattern Automaton (`aho_corasick.py`).
Builds a Trie + BFS Failure Links once in RAM and scans normalized Vietnamese strings in < 0.3 ms.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from apps.ai_risk_engine.lexicons_vi import (
    EXPLICIT_CRISIS,
    PROMPT_INJECTION_BYPASS,
    VN_GENZ_EUPHEMISMS,
)


@dataclass(frozen=True)
class PatternMeta:
    pattern: str
    category: str  # "EXPLICIT_CRISIS" | "VN_GENZ_EUPHEMISMS" | "PROMPT_INJECTION_BYPASS"
    weight: float
    direct_imminent: bool = False


@dataclass(frozen=True)
class ACMatch:
    pattern: str
    category: str
    weight: float
    direct_imminent: bool
    start: int
    end: int


class _TrieNode:
    __slots__ = ("children", "fail", "outputs")

    def __init__(self) -> None:
        self.children: dict[str, _TrieNode] = {}
        self.fail: _TrieNode | None = None
        self.outputs: list[PatternMeta] = []


class AhoCorasickAutomaton:
    def __init__(self, patterns: list[PatternMeta]) -> None:
        self.root = _TrieNode()
        for meta in patterns:
            if not meta.pattern:
                continue
            node = self.root
            for ch in meta.pattern:
                nxt = node.children.get(ch)
                if nxt is None:
                    nxt = _TrieNode()
                    node.children[ch] = nxt
                node = nxt
            node.outputs.append(meta)
        self._build_failure_links()

    def _build_failure_links(self) -> None:
        queue: deque[_TrieNode] = deque()
        self.root.fail = self.root
        for child in self.root.children.values():
            child.fail = self.root
            queue.append(child)

        while queue:
            current = queue.popleft()
            for ch, child in current.children.items():
                queue.append(child)
                fallback = current.fail
                while fallback is not self.root and ch not in fallback.children:
                    fallback = fallback.fail  # type: ignore[assignment]
                child.fail = fallback.children.get(ch, self.root) if fallback else self.root
                if child.fail is child:
                    child.fail = self.root
                if child.fail and child.fail.outputs:
                    child.outputs.extend(child.fail.outputs)

    @staticmethod
    def _is_word_boundary(text: str, start: int, end: int) -> bool:
        if start > 0 and text[start - 1].isalnum():
            return False
        if end < len(text) and text[end].isalnum():
            return False
        return True

    def search(self, text: str, whole_word: bool = True) -> list[ACMatch]:
        if not text:
            return []
        node = self.root
        matches: list[ACMatch] = []
        for idx, ch in enumerate(text):
            while node is not self.root and ch not in node.children:
                node = node.fail  # type: ignore[assignment]
            node = node.children.get(ch, self.root)
            if node.outputs:
                end_pos = idx + 1
                for meta in node.outputs:
                    start_pos = end_pos - len(meta.pattern)
                    if not whole_word or self._is_word_boundary(text, start_pos, end_pos):
                        matches.append(
                            ACMatch(
                                pattern=meta.pattern,
                                category=meta.category,
                                weight=meta.weight,
                                direct_imminent=meta.direct_imminent,
                                start=start_pos,
                                end=end_pos,
                            )
                        )
        return matches


@lru_cache(maxsize=1)
def get_crisis_automaton() -> AhoCorasickAutomaton:
    all_patterns: list[PatternMeta] = []
    for phrase, weight, direct_imminent in EXPLICIT_CRISIS:
        all_patterns.append(
            PatternMeta(
                pattern=phrase.lower().strip(),
                category="EXPLICIT_CRISIS",
                weight=weight,
                direct_imminent=direct_imminent,
            )
        )
    for phrase, weight in VN_GENZ_EUPHEMISMS:
        all_patterns.append(
            PatternMeta(
                pattern=phrase.lower().strip(),
                category="VN_GENZ_EUPHEMISMS",
                weight=weight,
                direct_imminent=False,
            )
        )
    for phrase, weight in PROMPT_INJECTION_BYPASS:
        all_patterns.append(
            PatternMeta(
                pattern=phrase.lower().strip(),
                category="PROMPT_INJECTION_BYPASS",
                weight=weight,
                direct_imminent=False,
            )
        )
    return AhoCorasickAutomaton(all_patterns)
