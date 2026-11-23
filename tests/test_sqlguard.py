import pytest

from app.sqlguard import UnsafeSQL, check

ALLOWED = ("lake.ec2.instances", "lake.ref.instance_types")


def ok(sql, max_rows=100):
    return check(sql, ALLOWED, max_rows)


def test_plain_select_gets_a_limit():
    out = ok("SELECT region, count(*) FROM ec2.instances GROUP BY 1")
    assert out.tables == ["lake.ec2.instances"]
    assert out.sql.endswith("LIMIT 100")


def test_smaller_limit_is_kept_and_larger_is_capped():
    assert ok("SELECT * FROM lake.ec2.instances LIMIT 5").sql.endswith("LIMIT 5")
    assert ok("SELECT * FROM lake.ec2.instances LIMIT 5000").sql.endswith("LIMIT 100")


def test_cte_and_join_against_allowed_tables():
    out = ok("""WITH latest AS (SELECT max(snapshot_ts) ts FROM instances)
                SELECT t.gpus, count(*) FROM instances i JOIN ref.instance_types t USING (instance_type)
                JOIN latest l ON i.snapshot_ts = l.ts GROUP BY 1""")
    assert out.tables == ["lake.ec2.instances", "lake.ref.instance_types"]


@pytest.mark.parametrize("sql", [
    "DELETE FROM lake.ec2.instances",
    "DROP TABLE lake.ec2.instances",
    "INSERT INTO lake.ec2.instances SELECT * FROM lake.ec2.instances",
    "CREATE TABLE lake.ec2.x AS SELECT 1 AS a",
    "SELECT 1; SELECT 2",
    "SELECT * FROM system.runtime.queries",
    "SELECT * FROM lake.information_schema.tables",
    "SELECT * FROM postgres.public.users",
    "SELECT 1",
    "SELEC * FROM instances",
])
def test_rejected(sql):
    with pytest.raises(UnsafeSQL):
        ok(sql)


def test_error_message_tells_the_model_what_it_can_use():
    with pytest.raises(UnsafeSQL, match="lake.ec2.instances"):
        ok("SELECT * FROM lake.ec2.secret_table")
