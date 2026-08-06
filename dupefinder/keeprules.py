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
