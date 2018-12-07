import pytest

from splitgraph.commands import commit, diff
from splitgraph.commands.info import get_image
from test.splitgraph.conftest import PG_MNT

# Test cases: ops are a list of operations (with commit after each set);
#             diffs are expected diffs produced by each operation.

CASES = [
    [  # Insert + update changed into a single insert
        ("""INSERT INTO "test/pg_mount".fruits VALUES (3, 'mayonnaise');
        UPDATE "test/pg_mount".fruits SET name = 'mustard' WHERE fruit_id = 3""",
         [((3, 'mustard'), 0, {'c': [], 'v': []})]),
        # Insert + update + delete did nothing (sequences not supported)
        ("""INSERT INTO "test/pg_mount".fruits VALUES (4, 'kumquat');
        UPDATE "test/pg_mount".fruits SET name = 'mustard' WHERE fruit_id = 4;
        DELETE FROM "test/pg_mount".fruits WHERE fruit_id = 4""",
         []),
        # delete + reinsert same results in nothing
        ("""DELETE FROM "test/pg_mount".fruits WHERE fruit_id = 1;
        INSERT INTO "test/pg_mount".fruits VALUES (1, 'apple')""",
         []),
        # Two updates, but the PK changed back to the original one -- no diff.
        ("""UPDATE "test/pg_mount".fruits SET name = 'pineapple' WHERE fruit_id = 1;
        UPDATE "test/pg_mount".fruits SET name = 'apple' WHERE fruit_id = 1""",
         [])
    ],
    [  # Now test this whole thing works with primary keys
        ("""ALTER TABLE "test/pg_mount".fruits ADD PRIMARY KEY (fruit_id)""",
         []),
        # Insert + update changed into a single insert (same pk, different value)
        ("""INSERT INTO "test/pg_mount".fruits VALUES (3, 'mayonnaise');
            UPDATE "test/pg_mount".fruits SET name = 'mustard' WHERE fruit_id = 3""",
         [((3,), 0, {'c': ['name'], 'v': ['mustard']})]),
        # Insert + update + delete did nothing
        ("""INSERT INTO "test/pg_mount".fruits VALUES (4, 'kumquat');
            UPDATE "test/pg_mount".fruits SET name = 'mustard' WHERE fruit_id = 4;
            DELETE FROM "test/pg_mount".fruits WHERE fruit_id = 4""",
         []),
        # delete + reinsert same
        ("""DELETE FROM "test/pg_mount".fruits WHERE fruit_id = 1;
            INSERT INTO "test/pg_mount".fruits VALUES (1, 'apple')""",
         # Currently the packer isn't aware that we rewrote the same value
         [((1,), 2, {'c': ['name'], 'v': ['apple']})]),
        # Two updates
        ("""UPDATE "test/pg_mount".fruits SET name = 'pineapple' WHERE fruit_id = 1;
            UPDATE "test/pg_mount".fruits SET name = 'apple' WHERE fruit_id = 1""",
         # Same here
         [((1,), 2, {'c': ['name'], 'v': ['apple']})]),
    ],
    [
        # Test a table with 2 PKs and 2 non-PK columns
        ("""DROP TABLE "test/pg_mount".fruits; CREATE TABLE "test/pg_mount".fruits (
            pk1 INTEGER,
            pk2 INTEGER,
            col1 VARCHAR,
            col2 VARCHAR,        
            PRIMARY KEY (pk1, pk2));
            INSERT INTO "test/pg_mount".fruits VALUES (1, 1, 'val1', 'val2')""", []),
        # Test an update touching part of the PK and part of the contents
        ("""UPDATE "test/pg_mount".fruits SET pk2 = 2, col1 = 'val3' WHERE pk1 = 1""",
         # FIXME this is _wrong_, we're supposed to output "delete (1, 1); insert (1,2) with col1=val3, col2=val2
         # (old value not preserved)
         [((1, 1), 1, None),
          ((1, 2), 0, {'c': ['col1'], 'v': ['val3']})]),
    ]
]


@pytest.mark.parametrize("test_case", CASES)
def test_diff_conflation_on_commit(sg_pg_conn, test_case):
    for operation, expected_diff in test_case:
        # Dump the operation we're running to stdout for easier debugging
        print("%r -> %r" % (operation, expected_diff))
        with sg_pg_conn.cursor() as cur:
            cur.execute(operation)
        sg_pg_conn.commit()
        head = commit(PG_MNT)
        assert diff(PG_MNT, 'fruits', get_image(PG_MNT, head).parent_id, head) == expected_diff
