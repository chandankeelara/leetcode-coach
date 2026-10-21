"""Static lookups: leetcode.com slugs, pattern names/tiers, default target minutes."""
import re

from .rules import TIER1, TIER2

# Titles in the old repo are often shortened ("LCA of a Binary Tree"). When the derived slug
# would be wrong, the number maps straight to the real leetcode.com slug.
SLUG_OVERRIDES = {
    236: "lowest-common-ancestor-of-a-binary-tree",
    98: "validate-binary-search-tree",
    103: "binary-tree-zigzag-level-order-traversal",
    17: "letter-combinations-of-a-phone-number",
    300: "longest-increasing-subsequence",
    153: "find-minimum-in-rotated-sorted-array",
    208: "implement-trie-prefix-tree",
    211: "design-add-and-search-words-data-structure",
    167: "two-sum-ii-input-array-is-sorted",
    323: "number-of-connected-components-in-an-undirected-graph",
    1482: "minimum-number-of-days-to-make-m-bouquets",
    1760: "minimum-limit-of-balls-in-a-bag",
    862: "shortest-subarray-with-sum-at-least-k",
    1011: "capacity-to-ship-packages-within-d-days",
    102: "binary-tree-level-order-traversal",
    1091: "shortest-path-in-binary-matrix",
    215: "kth-largest-element-in-an-array",
    703: "kth-largest-element-in-a-stream",
    973: "k-closest-points-to-origin",
    438: "find-all-anagrams-in-a-string",
    3: "longest-substring-without-repeating-characters",
    643: "maximum-average-subarray-i",
    121: "best-time-to-buy-and-sell-stock",
    23: "merge-k-sorted-lists",
    295: "find-median-from-data-stream",
    297: "serialize-and-deserialize-binary-tree",
    124: "binary-tree-maximum-path-sum",
    15: "3sum",
    1143: "longest-common-subsequence",
    10: "regular-expression-matching",
    496: "next-greater-element-i",
    503: "next-greater-element-ii",
}


def lc_slug(number, title):
    """leetcode.com slug: override by number, else lowercase and hyphenate the title."""
    if number in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[number]
    s = re.sub(r"[^a-z0-9 \-]", "", title.lower())
    return re.sub(r"[\s\-]+", "-", s).strip("-")


def leetcode_url(slug):
    return f"https://leetcode.com/problems/{slug}/"


def pattern_name(slug):
    return slug.replace("-", " ").title().replace("Dfs", "DFS").replace("Bfs", "BFS")


def pattern_tier(slug):
    if slug in TIER1:
        return 1
    if slug in TIER2:
        return 2
    return 3


# Target minutes handed out with a problem, by difficulty (matches the targets in attempts.csv:
# Easy 8-12, Medium 12-20, Hard 25-40). Difficulty is internal and never shown.
DEFAULT_TARGET = {"E": 10, "M": 15, "H": 35}


def default_target(difficulty):
    return DEFAULT_TARGET.get(difficulty, 15)
