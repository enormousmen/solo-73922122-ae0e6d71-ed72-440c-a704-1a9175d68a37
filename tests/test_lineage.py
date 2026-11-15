"""列级血缘端到端测试。

覆盖：聚合/JOIN/子查询/单层 CTE 的列追溯、别名与同名列作用域区分、
无法确定分支的断点语义、失败节点处理、以及“修改/删除模型不改变旧批次
追溯结果”的冻结保证。所有追溯只读取 run_nodes 中冻结的 SQL 与快照表。
"""
import time

import pytest

from conftest import conn as _new_conn


def _conn():
    return _new_conn()


def _wait_terminal(client, run_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/runs/{run_id}").json()
        if d["run"]["status"] in ("succeeded", "failed"):
            return d
        time.sleep(0.05)
    raise AssertionError("run did not finish in time")


def _clean_db():
    c = _conn()
    cur = c.cursor()
    cur.execute("SELECT snapshot_schema FROM public.runs")
    for (schema,) in cur.fetchall():
        if schema:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    cur.execute("DELETE FROM public.run_events")
    cur.execute("DELETE FROM public.run_nodes")
    cur.execute("DELETE FROM public.runs")
    cur.execute("DELETE FROM public.models")
    cur.execute("ALTER SEQUENCE models_id_seq RESTART WITH 1")
    cur.execute("ALTER SEQUENCE runs_id_seq RESTART WITH 1")
    c.commit()
    c.close()


@pytest.fixture(autouse=True)
def clean_lineage():
    _clean_db()
    yield
    _clean_db()


def _create(client, name, sql, upstreams=None):
    r = client.post("/api/models", json={
        "name": name, "sql_text": sql, "upstream_ids": upstreams or []})
    assert r.status_code == 201, r.json()
    return r.json()["id"]


def _start_and_wait(client):
    run_id = client.post("/api/runs").json()["run_id"]
    return run_id, _wait_terminal(client, run_id)


def _node_id(cols, name):
    return next(n["node_id"] for n in cols["nodes"] if n["model_name"] == name)


def _trace(client, run_id, node_id, column):
    return client.get(
        f"/api/runs/{run_id}/nodes/{node_id}/lineage",
        params={"column": column}).json()


def _targets(trace):
    return {(e["target_type"], e["target"]) for e in trace["edges"]}


# ---------------------------------------------------------------------------
def test_seed_net_amount_traces_to_sales_and_refunds(client):
    """预置两模型：净额列经 JOIN+子查询+聚合追到 sales.amount 与 refunds.refund_amount。"""
    _create(client, "daily_sales_clean",
            "SELECT store_id, biz_date, SUM(amount) AS gross_amount "
            "FROM sales GROUP BY store_id, biz_date")
    net_id = _create(
        client, "daily_net_sales",
        "SELECT s.store_id, s.biz_date, "
        "s.gross_amount - COALESCE(r.refund_amount, 0) AS net_amount "
        "FROM daily_sales_clean s LEFT JOIN ("
        "SELECT store_id, biz_date, SUM(refund_amount) AS refund_amount "
        "FROM refunds GROUP BY store_id, biz_date"
        ") r ON r.store_id=s.store_id AND r.biz_date=s.biz_date",
        upstreams=[1])

    run_id, detail = _start_and_wait(client)
    assert detail["run"]["status"] == "succeeded"
    cols = client.get(f"/api/runs/{run_id}/lineage/columns").json()
    trace = _trace(client, run_id, _node_id(cols, "daily_net_sales"), "net_amount")

    assert trace["complete"] is True
    assert trace["blocked"] == []
    leaves = {(f["table"], f["column"]) for f in trace["source_fields"]}
    assert leaves == {("sales", "amount"), ("refunds", "refund_amount")}

    # 每段边的关键属性
    by_target = {e["target"]: e for e in trace["edges"]}
    assert by_target["daily_sales_clean.gross_amount"]["relation_kind"] == "model"
    assert by_target["daily_sales_clean.gross_amount"]["via_alias"] == "s"
    assert by_target["sales.amount"]["transform"] == "aggregate"
    assert by_target["refunds.refund_amount"]["transform"] == "aggregate"
    # 子查询作为独立一段被记录
    assert any(e["target_type"] == "subquery" for e in trace["edges"])
    # 深度：模型->模型 为 0，模型内部->源字段为 1，子查询内部->源字段为 1
    assert {e["depth"] for e in trace["edges"]} == {0, 1}


def test_aggregate_group_by_column_lineage(client):
    _create(client, "agg_model",
            "SELECT store_id, biz_date, SUM(amount) AS gross_amount, "
            "COUNT(*) AS cnt FROM sales GROUP BY store_id, biz_date")
    run_id, _ = _start_and_wait(client)
    cols = client.get(f"/api/runs/{run_id}/lineage/columns").json()
    nid = _node_id(cols, "agg_model")

    gross = _trace(client, run_id, nid, "gross_amount")
    assert gross["source_fields"] == [{"table": "sales", "column": "amount"}]
    assert gross["edges"][0]["transform"] == "aggregate"

    # COUNT(*) 不引用任何列：常量/无列函数断点，不猜测
    cnt = _trace(client, run_id, nid, "cnt")
    assert cnt["complete"] is False
    assert cnt["blocked"] and "不引用任何列" in cnt["blocked"][0]["reason"]

    # GROUP BY 的键列是 direct 映射
    sid = _trace(client, run_id, nid, "store_id")
    assert sid["source_fields"] == [{"table": "sales", "column": "store_id"}]
    assert sid["edges"][0]["transform"] == "direct"


def test_single_level_cte_lineage(client):
    _create(client, "cte_model",
            "WITH agg AS ("
            "SELECT store_id, SUM(amount) AS ga FROM sales GROUP BY store_id"
            ") SELECT a.store_id, a.ga AS gross FROM agg a")
    run_id, _ = _start_and_wait(client)
    cols = client.get(f"/api/runs/{run_id}/lineage/columns").json()
    nid = _node_id(cols, "cte_model")

    trace = _trace(client, run_id, nid, "gross")
    assert trace["complete"] is True
    assert trace["source_fields"] == [{"table": "sales", "column": "amount"}]
    kinds = [e["target_type"] for e in trace["edges"]]
    assert "cte" in kinds  # 先经过 CTE 段，再进入 CTE 内部
    assert any(e["target"] == "agg.ga" for e in trace["edges"])

    sid = _trace(client, run_id, nid, "store_id")
    assert sid["source_fields"] == [{"table": "sales", "column": "store_id"}]


def test_alias_and_same_name_column_scope(client):
    """别名限定可解析；同名列裸引用有歧义时必须报断点，不能猜测。"""
    _create(client, "scope_model",
            "SELECT store_id, s.amount AS amt, r.refund_amount AS rf "
            "FROM sales s JOIN refunds r USING (store_id)")
    run_id, _ = _start_and_wait(client)
    cols = client.get(f"/api/runs/{run_id}/lineage/columns").json()
    nid = _node_id(cols, "scope_model")

    # 限定名 s.amount / r.refund_amount 精确解析
    amt = _trace(client, run_id, nid, "amt")
    assert amt["source_fields"] == [{"table": "sales", "column": "amount"}]
    rf = _trace(client, run_id, nid, "rf")
    assert rf["source_fields"] == [{"table": "refunds", "column": "refund_amount"}]

    # 裸 store_id 在两表 USING 合并后仍存在同名来源 -> 歧义断点
    sid = _trace(client, run_id, nid, "store_id")
    assert sid["complete"] is False
    assert sid["blocked"] and "无法确定来源" in sid["blocked"][0]["reason"]
    assert "store_id" in sid["blocked"][0]["reason"]


def test_unknown_branch_reports_stop_model_and_reason(client):
    """窗口函数/集合操作等超出支持范围：断点写明停在哪个模型及原因。"""
    _create(client, "win_model",
            "SELECT store_id, ROW_NUMBER() OVER (PARTITION BY store_id) AS rn "
            "FROM sales")
    run_id, _ = _start_and_wait(client)
    cols = client.get(f"/api/runs/{run_id}/lineage/columns").json()
    nid = _node_id(cols, "win_model")
    trace = _trace(client, run_id, nid, "rn")
    assert trace["complete"] is False
    assert trace["blocked"]
    b = trace["blocked"][0]
    assert b["model"] == "win_model"
    assert "窗口函数" in b["reason"]

    _create(client, "union_model",
            "SELECT store_id FROM sales UNION SELECT store_id FROM refunds")
    run_id2, _ = _start_and_wait(client)
    cols2 = client.get(f"/api/runs/{run_id2}/lineage/columns").json()
    nid2 = _node_id(cols2, "union_model")
    t2 = _trace(client, run_id2, nid2, "store_id")
    assert t2["complete"] is False
    assert any("集合操作" in b["reason"] for b in t2["blocked"])


def test_failed_upstream_blocks_lineage_with_reason(client):
    fail_id = _create(client, "failed_model",
                      "SELECT store_id, no_such_col AS x FROM sales")
    _create(client, "downstream",
            "SELECT store_id, x FROM failed_model", upstreams=[fail_id])
    run_id, detail = _start_and_wait(client)
    assert detail["run"]["status"] == "failed"

    cols = client.get(f"/api/runs/{run_id}/lineage/columns").json()
    down = next(n for n in cols["nodes"] if n["model_name"] == "downstream")
    # 未成功节点没有可追溯结果列
    assert down["columns"] == []
    # 显式追溯不存在的结果列 -> 404，而不是编造
    r = client.get(f"/api/runs/{run_id}/nodes/{down['node_id']}/lineage",
                   params={"column": "x"})
    assert r.status_code == 404


def test_changing_models_after_run_does_not_change_old_lineage(client):
    """关键冻结保证：改 SQL、删模型后，旧批次追溯结果保持不变。"""
    clean_id = _create(
        client, "daily_sales_clean",
        "SELECT store_id, biz_date, SUM(amount) AS gross_amount "
        "FROM sales GROUP BY store_id, biz_date")
    _create(
        client, "daily_net_sales",
        "SELECT s.store_id, s.gross_amount - COALESCE(r.ra, 0) AS net_amount "
        "FROM daily_sales_clean s LEFT JOIN ("
        "SELECT store_id, SUM(refund_amount) AS ra FROM refunds GROUP BY store_id"
        ") r ON r.store_id=s.store_id",
        upstreams=[clean_id])

    run_id, detail = _start_and_wait(client)
    assert detail["run"]["status"] == "succeeded"
    cols = client.get(f"/api/runs/{run_id}/lineage/columns").json()
    net_node = _node_id(cols, "daily_net_sales")
    before = _trace(client, run_id, net_node, "net_amount")
    assert before["complete"] is True
    before_edges = [(e["from_model"], e["from_column"], e["target"])
                    for e in before["edges"]]
    before_sql = before["frozen_sql"]

    # 批次结束后把上游 SQL 改写成完全不同的口径
    r = client.patch(f"/api/models/{clean_id}/sql", json={
        "sql_text": "SELECT store_id, MAX(amount) AS gross_amount "
                    "FROM sales GROUP BY store_id"})
    assert r.status_code == 200

    after = _trace(client, run_id, net_node, "net_amount")
    assert after["frozen_sql"] == before_sql          # 仍是冻结 SQL
    assert [(e["from_model"], e["from_column"], e["target"])
            for e in after["edges"]] == before_edges
    assert after["source_fields"] == before["source_fields"]

    # 删除下游模型（无其他依赖时可删），旧批次节点仍可追溯
    models = {m["name"]: m["id"] for m in client.get("/api/models").json()}
    r = client.delete(f"/api/models/{models['daily_net_sales']}")
    assert r.status_code == 204
    again = _trace(client, run_id, net_node, "net_amount")
    assert again["complete"] is True
    assert again["source_fields"] == before["source_fields"]
    assert again["model_name"] == "daily_net_sales"


def test_lineage_columns_endpoint_lists_only_succeeded(client):
    _create(client, "ok_model",
            "SELECT store_id, SUM(amount) AS g FROM sales GROUP BY store_id")
    _create(client, "bad_model",
            "SELECT store_id, missing_col FROM sales")
    run_id, _ = _start_and_wait(client)
    cols = client.get(f"/api/runs/{run_id}/lineage/columns").json()
    statuses = {n["model_name"]: n for n in cols["nodes"]}
    assert statuses["ok_model"]["columns"] == ["store_id", "g"]
    assert statuses["bad_model"]["columns"] == []
    # 不存在的批次/节点
    assert client.get("/api/runs/99999/lineage/columns").status_code == 404


def test_cte_column_alias_rename(client):
    """WITH c(sid, x) 的列别名按位置重命名 CTE 输出，来源保持不变。"""
    _create(client, "cte_alias_model",
            "WITH c(sid, x) AS (SELECT store_id, amount FROM sales) "
            "SELECT sid, x FROM c")
    run_id, _ = _start_and_wait(client)
    cols = client.get(f"/api/runs/{run_id}/lineage/columns").json()
    nid = _node_id(cols, "cte_alias_model")

    sid = _trace(client, run_id, nid, "sid")
    assert sid["complete"] is True
    assert sid["source_fields"] == [{"table": "sales", "column": "store_id"}]
    x = _trace(client, run_id, nid, "x")
    assert x["source_fields"] == [{"table": "sales", "column": "amount"}]


def test_complex_cte_join_subquery_net_lineage(client):
    """综合净额口径：CTE 聚合销售 + JOIN 子查询聚合退款，净额列追到两个源字段。"""
    _create(client, "net_report",
            "WITH sales_agg AS ("
            "SELECT store_id, SUM(amount) AS gross FROM sales GROUP BY store_id"
            ") "
            "SELECT s.store_id, "
            "s.gross - COALESCE(r.refund_agg, 0) AS net, "
            "s.gross AS gross_amt "
            "FROM sales_agg s "
            "LEFT JOIN (SELECT store_id, SUM(refund_amount) AS refund_agg "
            "FROM refunds GROUP BY store_id) r "
            "ON r.store_id = s.store_id")
    run_id, _ = _start_and_wait(client)
    cols = client.get(f"/api/runs/{run_id}/lineage/columns").json()
    nid = _node_id(cols, "net_report")

    net = _trace(client, run_id, nid, "net")
    assert net["complete"] is True
    leaves = {(f["table"], f["column"]) for f in net["source_fields"]}
    assert leaves == {("sales", "amount"), ("refunds", "refund_amount")}
    # CTE 段与子查询段都被显式记录
    kinds = {e["target_type"] for e in net["edges"]}
    assert {"cte", "subquery", "source_field"} <= kinds
    # 聚合转换被标注
    assert all(e["transform"] == "aggregate"
               for e in net["edges"] if e["target_type"] == "source_field")

    gross = _trace(client, run_id, nid, "gross_amt")
    assert gross["source_fields"] == [{"table": "sales", "column": "amount"}]


def test_sql_guard_accepts_multiline_and_multi_cte(client):
    """守卫对多行 CTE（含嵌套括号）、多 CTE、带列别名 CTE 的兼容性。"""
    # 多行 + 内部聚合括号：旧实现会误判 WITH 结构
    r = client.post("/api/models", json={
        "name": "multiline_cte",
        "sql_text": "WITH agg AS (\n"
                    "  SELECT store_id, SUM(amount) AS ga\n"
                    "  FROM sales GROUP BY store_id\n"
                    ")\nSELECT store_id, ga FROM agg",
        "upstream_ids": []})
    assert r.status_code == 201, r.json()
    # 多个逗号分隔的 CTE，第二个 CTE 名必须被识别为非物理表
    r = client.post("/api/models", json={
        "name": "multi_cte",
        "sql_text": "WITH x AS (SELECT store_id FROM sales), "
                    "y AS (SELECT store_id FROM refunds) "
                    "SELECT x.store_id FROM x JOIN y ON x.store_id=y.store_id",
        "upstream_ids": []})
    assert r.status_code == 201, r.json()
    # 带列别名列表的 CTE
    r = client.post("/api/models", json={
        "name": "aliased_cte",
        "sql_text": "WITH c(sid, x) AS (SELECT store_id, amount FROM sales) "
                    "SELECT sid, x FROM c",
        "upstream_ids": []})
    assert r.status_code == 201, r.json()


def test_scalar_subquery_in_select_contributes_lineage(client):
    _create(client, "scalar_model",
            "SELECT s.store_id, "
            "(SELECT MAX(r.refund_amount) FROM refunds r "
            " WHERE r.store_id = s.store_id) AS max_refund "
            "FROM sales s GROUP BY s.store_id")
    run_id, _ = _start_and_wait(client)
    cols = client.get(f"/api/runs/{run_id}/lineage/columns").json()
    nid = _node_id(cols, "scalar_model")
    trace = _trace(client, run_id, nid, "max_refund")
    assert trace["complete"] is True
    leaves = {(f["table"], f["column"]) for f in trace["source_fields"]}
    # 标量子查询内引用 refunds.refund_amount；外层 WHERE 关联的 sales.store_id 也被记录
    assert ("refunds", "refund_amount") in leaves
