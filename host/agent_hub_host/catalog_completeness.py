"""Completeness verdicts for row-bounded native session catalogs."""
import re


_NATIVE_ID = re.compile(r'^[a-zA-Z0-9-]{1,160}$')


def census_is_complete(all_rows, bounded_rows, *, limit, confirmed_exit):
    """Return whether a bounded report includes every live or ambiguous native.

    ``all_rows`` is the full host catalog before truncation. The host supplies
    its positive native-exit check; every omitted row must pass that check,
    even if its reported phase looks terminal. Selected rows are already in
    the bounded report and need no second exit check here.
    """
    if type(limit) is not int or limit <= 0:
        raise ValueError('limit must be a positive integer')
    if not isinstance(all_rows, list) or not isinstance(bounded_rows, list):
        return False

    def identities(rows):
        values = []
        for row in rows:
            if not isinstance(row, dict):
                return None
            native_id = row.get('native_id') if 'native_id' in row else row.get('id')
            if not isinstance(native_id, str) or not _NATIVE_ID.fullmatch(native_id):
                return None
            if 'native_id' in row and 'id' in row and row['id'] != native_id:
                return None
            values.append(native_id)
        return values if len(set(values)) == len(values) else None

    all_ids = identities(all_rows)
    bounded_ids = identities(bounded_rows)
    if all_ids is None or bounded_ids is None or len(bounded_rows) > limit:
        return False
    all_id_set, bounded_id_set = set(all_ids), set(bounded_ids)
    if not bounded_id_set.issubset(all_id_set):
        return False
    if len(all_rows) <= limit:
        return len(bounded_rows) == len(all_rows) and bounded_id_set == all_id_set

    for row in all_rows:
        native_id = row.get('native_id')
        if not isinstance(native_id, str):
            native_id = row.get('id')
        if native_id in bounded_id_set:
            continue
        if confirmed_exit(row) is not True:
            return False
    return True
