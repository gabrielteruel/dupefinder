"""Pure logic for dedupe mode: grouping duplicates and choosing which copy to keep.

No filesystem access, no server state, no clock -- every function here is a
plain transformation over the data comparer.compare() already produced. See
docs/superpowers/specs/2026-08-05-dedupe-mode-design.md section 5 for the
reasoning behind the tie-break rules.
"""

from collections import defaultdict

from dupefinder.models import DuplicateGroup, ReportRow


def group_duplicates(
    rows: list[ReportRow],
) -> tuple[list[DuplicateGroup], DuplicateGroup | None]:
    """Group rows by sha256 into DuplicateGroups, and split out the zero-byte group.

    A row with sha256 is None was already proven unique by the fast path (a
    size bucket with no collision needs no hashing at all) and can never be a
    duplicate of anything, so it is excluded here rather than forming a
    meaningless group of one.

    Groups with only one member are dropped: a "duplicate" group needs 2+.

    The zero-byte group (size == 0, if present) is returned separately as
    `empty_group` instead of being included in `groups`. All zero-byte files
    share one SHA-256 by construction, so they would otherwise form one
    enormous group with nothing meaningful to preselect -- callers render it
    with no preselection (see keeprules.resolve(), which excludes it too).

    Members within a group are sorted by rel_path. Groups are sorted by
    wasted_bytes descending, so the biggest reclaimable win sorts first.
    """
    by_digest: dict[str, list[ReportRow]] = defaultdict(list)
    for row in rows:
        if row.sha256 is None:
            continue
        by_digest[row.sha256].append(row)

    groups: list[DuplicateGroup] = []
    empty_group: DuplicateGroup | None = None

    for digest, members in by_digest.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda r: r.rel_path)
        group = DuplicateGroup(digest=digest, size=members[0].size, members=members)
        if group.size == 0:
            empty_group = group
        else:
            groups.append(group)

    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups, empty_group


def _matches(rel_path: str, rule: str) -> bool:
    """True when rel_path falls inside the `rule` subtree.

    Matching is by path component, not by string prefix: rule "fotos" must
    match "fotos/bici/img.png" but not "fotos2/img.png". A plain
    rel_path.startswith(rule) check would wrongly match the latter.
    """
    rule_parts = [p for p in rule.split("/") if p]
    if not rule_parts:
        return False  # an empty/root rule is rejected at the API boundary
    return rel_path.split("/")[: len(rule_parts)] == rule_parts


def resolve(groups: list[DuplicateGroup], keep_rules: list[str]) -> dict[str, str]:
    """Map each group's digest to the rel_path of the member to KEEP.

    `keep_rules` is an ordered list of folder subtrees, highest priority
    first: index 0 beats index 1, etc. For each member, its rank is the
    index of the first rule it matches, or len(keep_rules) (i.e. "no match",
    sorting after every real rule) if none match.

    The kept member is the one minimising:

        (rule_rank, path_depth, rel_path)

    - rule_rank  : lowest matching rule index wins
    - path_depth : number of path components -- the copy closest to the
                   scan root wins when no rule decides
    - rel_path   : lexicographic, the final, always-total tie-break

    All three keys are total functions of the member alone, so this always
    terminates in a single deterministic winner. Determinism matters: the
    result is what apply() moves and what the audit report records, and it
    must be reproducible after the fact from the same inputs.

    Zero-byte groups get no entry in the returned dict: the caller (the UI)
    renders that group with nothing preselected instead of picking one for it.
    """
    kept: dict[str, str] = {}
    for group in groups:
        if group.size == 0:
            continue

        def sort_key(member: ReportRow) -> tuple[int, int, str]:
            rank = next(
                (i for i, rule in enumerate(keep_rules) if _matches(member.rel_path, rule)),
                len(keep_rules),
            )
            depth = member.rel_path.count("/")
            return (rank, depth, member.rel_path)

        winner = min(group.members, key=sort_key)
        kept[group.digest] = winner.rel_path
    return kept
