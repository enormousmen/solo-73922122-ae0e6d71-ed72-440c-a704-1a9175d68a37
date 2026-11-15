"""列级血缘服务：基于批次冻结 SQL 与快照结果表做静态列追溯。

核心原则
========
1. **只读冻结数据**：所有 SQL 取自 ``run_nodes.sql_text``（批次启动时冻结），
   列清单取自快照 schema 内的结果表 / 源表。之后修改或删除 ``models`` 中的
   模型定义，对历史批次的追溯结果没有任何影响。
2. **不猜测**：任何无法在支持范围内唯一确定来源的列，都生成 ``resolved=false``
   的断点（``blocked``），写明停在哪个模型（或子查询/CTE）及原因。
3. **作用域区分**：表别名与同名列按 SQL 作用域解析——限定名 ``别名.列`` 只在
   该别名指向的关系里查找；裸列名在当前查询全部 FROM/JOIN 关系的输出列中
   查找；重名且未限定的裸列按“无法确定（歧义）”处理。

支持范围：普通 SELECT、聚合（含 GROUP BY）、JOIN、FROM/SELECT 标量子查询、
单层 CTE。集合操作、窗口函数、嵌套 CTE、``t.*`` 等无法确定的结构产生断点。

返回的每条 ``edges`` 记录描述一段引用关系：
``from_model.from_column --(via 别名/关系, transform)--> 目标列``，
``target_type`` 为 source_field / model_column / cte / subquery / 常量 / 未知。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .. import db as dbmod
from ..config import SOURCE_TABLES
from .sql_parser import ParsedQuery, Ref, Relation, SelectItem, parse

log = logging.getLogger("lineage")


class LineageError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class ColumnSource:
    """一个输出列在某个关系（表/子查询/CTE）中的直接来源。"""
    kind: str                       # source/model/cte/subquery/literal/ambiguous/unknown
    name: str | None = None         # 源表名 / 模型名 / CTE 名
    alias: str | None = None        # 本查询中使用的关系别名
    column: str | None = None       # 该关系中的列名
    reason: str | None = None
    transform: str | None = None    # direct/aggregate/expression/star
    relation: Relation | None = None  # 指向具体关系，供继续递归


@dataclass
class Scope:
    """一个查询（模型顶层 / 子查询 / CTE）解析后的作用域。"""
    label: str
    relations: list[Relation] = field(default_factory=list)
    outputs: dict[str, list[ColumnSource]] = field(default_factory=dict)
    unsupported_reason: str | None = None
    cte_scopes: dict[str, "Scope"] = field(default_factory=dict)
    owner_node: NodeContext | None = None
    parent: "Scope | None" = None          # 标量子查询的外层作用域
    extra_refs: list[ColumnSource] = field(default_factory=list)  # 关联外层的引用


@dataclass
class NodeContext:
    node_id: int
    model_name: str
    sql_text: str
    status: str
    result_table: str | None
    upstream_node_ids: list[int]


# ---------------------------------------------------------------------------
# 批次加载
# ---------------------------------------------------------------------------
def _load_run(conn, run_id: int) -> dict:
    row = dbmod.fetch_one(conn, "SELECT * FROM public.runs WHERE id=%s", (run_id,))
    if row is None:
        raise LineageError(f"试跑 #{run_id} 不存在")
    return dict(row)


def _load_nodes(conn, run_id: int) -> list[NodeContext]:
    rows = dbmod.fetch_all(
        conn,
        "SELECT id, model_id, model_name, sql_text, status, result_table, "
        "upstream_ids FROM public.run_nodes WHERE run_id=%s ORDER BY position",
        (run_id,))
    return [
        NodeContext(
            node_id=r["id"], model_name=r["model_name"], sql_text=r["sql_text"],
            status=r["status"], result_table=r["result_table"],
            upstream_node_ids=list(r["upstream_ids"] or []))
        for r in rows
    ]


def _table_columns(conn, qualified_table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {qualified_table} LIMIT 0")
        return [d[0].lower() for d in cur.description]


# ---------------------------------------------------------------------------
# 作用域构建
# ---------------------------------------------------------------------------
def _build_scope(
    parsed: ParsedQuery,
    label: str,
    node: NodeContext,
    nodes_by_name: dict[str, NodeContext],
    conn,
    schema: str,
    cte_scopes: dict[str, Scope],
    parent: Scope | None = None,
) -> Scope:
    scope = Scope(label=label, unsupported_reason=parsed.unsupported_reason,
                  cte_scopes=cte_scopes, owner_node=node, parent=parent)
    scope.relations = list(parsed.relations)

    rel_columns: dict[str, list[str]] = {}
    for rel in parsed.relations:
        rel_columns[rel.alias] = _relation_output_columns(
            rel, node, nodes_by_name, conn, schema, cte_scopes, scope)

    for item in parsed.items:
        if item.is_star:
            _, owners_by_col = _expand_star(item, parsed, rel_columns)
            for col, owners in owners_by_col.items():
                scope.outputs[col] = _star_sources(col, owners)
            continue
        out_name = (item.alias or item.implicit_name or "").lower()
        if not out_name:
            continue
        scope.outputs[out_name] = _resolve_item_sources(
            item, parsed, rel_columns, node, nodes_by_name, conn,
            schema, cte_scopes, parent, scope)
    return scope


def _relation_output_columns(
    rel: Relation,
    node: NodeContext,
    nodes_by_name: dict[str, NodeContext],
    conn,
    schema: str,
    cte_scopes: dict[str, Scope],
    parent: Scope | None = None,
) -> list[str]:
    if rel.kind == "table":
        try:
            return _table_columns(conn, f"{schema}.{rel.name}")
        except Exception as exc:
            log.info("lineage: 关系 %s.%s 不可读: %s", schema, rel.name, exc)
            return []
    if rel.kind == "cte":
        sub_scope = cte_scopes.get(rel.name)
        return list(sub_scope.outputs.keys()) if sub_scope else []
    if rel.kind == "subquery" and rel.subquery is not None:
        sub_scope = _build_scope(
            rel.subquery, f"子查询 {rel.alias}", node, nodes_by_name,
            conn, schema, cte_scopes, parent=parent)
        return list(sub_scope.outputs.keys())
    return []


def _expand_star(
    item: SelectItem,
    parsed: ParsedQuery,
    rel_columns: dict[str, list[str]],
) -> tuple[list[str], dict[str, list[Relation]]]:
    ordered: list[str] = []
    owners: dict[str, list[Relation]] = {}
    for rel in parsed.relations:
        if item.star_alias is not None and rel.alias != item.star_alias:
            continue
        for col in rel_columns.get(rel.alias, []):
            owners.setdefault(col, []).append(rel)
            if col not in ordered:
                ordered.append(col)
    return ordered, owners


def _star_sources(column: str, owners: list[Relation]) -> list[ColumnSource]:
    if len(owners) == 1:
        rel = owners[0]
        kind = ("source" if rel.kind == "table" and rel.name in SOURCE_TABLES
                else "model" if rel.kind == "table"
                else "cte" if rel.kind == "cte" else "subquery")
        return [ColumnSource(kind=kind, name=rel.name, alias=rel.alias,
                             column=column, transform="star", relation=rel)]
    if len(owners) > 1:
        return [ColumnSource(
            kind="ambiguous", column=column, transform="star",
            reason=f"列 {column} 同时存在于多个关系 "
                   f"{', '.join(o.alias for o in owners)}，"
                   "星号展开后无法区分来源")]
    return [ColumnSource(kind="unknown", column=column, transform="star",
                         reason="星号展开未找到对应关系列")]


def _resolve_item_sources(
    item: SelectItem,
    parsed: ParsedQuery,
    rel_columns: dict[str, list[str]],
    node: NodeContext,
    nodes_by_name: dict[str, NodeContext],
    conn,
    schema: str,
    cte_scopes: dict[str, Scope],
    parent_scope: Scope | None,
    current_scope: Scope,
) -> list[ColumnSource]:
    if parsed.unsupported_reason:
        return [ColumnSource(kind="unknown", reason=parsed.unsupported_reason,
                             transform=_transform_of(item))]

    transform = _transform_of(item)
    sources: list[ColumnSource] = []
    seen: set[tuple] = set()

    def add(src: ColumnSource) -> None:
        key = (src.kind, src.name, src.alias, src.column, src.reason, src.transform)
        if key not in seen:
            seen.add(key)
            sources.append(src)

    # SELECT 中的标量子查询：在它自己的作用域内完整解析（含关联外层引用），
    # 其父作用域就是当前查询作用域；子查询输出列来源 + 关联外层引用共同构成
    # 本输出列的来源。
    for sub in item.subqueries:
        sub_scope = _build_scope(
            sub, "SELECT 标量子查询", node, nodes_by_name, conn, schema,
            cte_scopes, parent=current_scope)
        if sub.unsupported_reason:
            add(ColumnSource(kind="unknown", reason=sub.unsupported_reason,
                             transform=transform))
        for inner_sources in sub_scope.outputs.values():
            for src in inner_sources:
                add(src)
        for src in sub_scope.extra_refs:
            add(src)

    for ref in item.refs:
        for src in _resolve_ref(ref, parsed, rel_columns, node, nodes_by_name,
                                conn, schema, cte_scopes, parent_scope,
                                current_scope):
            src.transform = transform
            add(src)

    if not sources:
        add(ColumnSource(kind="literal",
                         reason="表达式不引用任何列（常量/字面量/无列函数计算）",
                         transform=transform))
    return sources


def _transform_of(item: SelectItem) -> str:
    if item.is_star:
        return "star"
    if item.has_aggregate:
        return "aggregate"
    if item.funcs or len(item.refs) > 1 or item.subqueries:
        return "expression"
    return "direct"


def _resolve_ref(
    ref: Ref,
    parsed: ParsedQuery,
    rel_columns: dict[str, list[str]],
    node: NodeContext,
    nodes_by_name: dict[str, NodeContext],
    conn,
    schema: str,
    cte_scopes: dict[str, Scope],
    parent_scope: Scope | None,
    current_scope: Scope,
) -> list[ColumnSource]:
    if ref.alias is not None:
        rel = next((r for r in parsed.relations if r.alias == ref.alias), None)
        if rel is None:
            # 关联引用：别名属于外层查询（标量子查询 WHERE 中引用外层表）
            correlated = _resolve_in_parent(ref, parent_scope, current_scope)
            if correlated is not None:
                return correlated
            return [ColumnSource(
                kind="unknown", alias=ref.alias, column=ref.column,
                reason=f"别名 {ref.alias} 在当前查询作用域中不存在")]
        return _source_via_relation(rel, ref.column, node, nodes_by_name,
                                    conn, schema, cte_scopes, current_scope)

    owners = [rel for rel in parsed.relations
              if ref.column in rel_columns.get(rel.alias, [])]
    if len(owners) == 1:
        return _source_via_relation(owners[0], ref.column, node, nodes_by_name,
                                    conn, schema, cte_scopes, current_scope)
    if len(owners) > 1:
        return [ColumnSource(
            kind="ambiguous", column=ref.column,
            reason=f"裸列名 {ref.column} 在多个关系 "
                   f"{', '.join(o.alias for o in owners)} 中存在同名列，"
                   "SQL 未用别名限定，无法确定来源")]
    # 当前作用域解析不到：尝试父作用域（裸列的关联引用）
    correlated = _resolve_in_parent(ref, parent_scope, current_scope)
    if correlated is not None:
        return correlated
    unreadable = [rel.alias for rel in parsed.relations
                  if not rel_columns.get(rel.alias)]
    if unreadable:
        return [ColumnSource(
            kind="unknown", column=ref.column,
            reason=f"列 {ref.column} 无法定位；关系 {', '.join(unreadable)} "
                   "的输出列清单不可读（对应节点未成功或结果表缺失）")]
    return [ColumnSource(
        kind="unknown", column=ref.column,
        reason=f"列 {ref.column} 在当前查询任何关系的输出列中都不存在")]


def _resolve_in_parent(ref: Ref, parent_scope: Scope | None,
                       child_scope: Scope) -> list[ColumnSource] | None:
    """在父作用域中解析关联引用；父作用域也没有则返回 None。

    限定名（带别名）只匹配父作用域中该别名关系贡献的输出来源；
    裸列名在父作用域的同名输出列中查找。
    """
    if parent_scope is None:
        return None
    out = parent_scope.outputs.get(ref.column)
    if not out:
        return None
    if ref.alias is not None:
        matched = [src for src in out if src.alias == ref.alias]
        if not matched:
            return None
    else:
        matched = list(out)
    # 登记到子查询的关联引用集合（按值去重，避免 dataclass 含 Relation 的比较问题）
    existing = {(s.kind, s.name, s.alias, s.column) for s in child_scope.extra_refs}
    for src in matched:
        key = (src.kind, src.name, src.alias, src.column)
        if key not in existing:
            child_scope.extra_refs.append(src)
            existing.add(key)
    return matched


def _source_via_relation(
    rel: Relation,
    column: str,
    node: NodeContext,
    nodes_by_name: dict[str, NodeContext],
    conn,
    schema: str,
    cte_scopes: dict[str, Scope],
    parent_scope: Scope | None = None,
) -> list[ColumnSource]:
    if rel.kind == "table":
        kind = "source" if rel.name in SOURCE_TABLES else "model"
        return [ColumnSource(kind=kind, name=rel.name, alias=rel.alias,
                             column=column, relation=rel)]
    if rel.kind == "cte":
        sub_scope = cte_scopes.get(rel.name)
        if sub_scope is None:
            return [ColumnSource(kind="unknown", name=rel.name, alias=rel.alias,
                                 column=column, relation=rel,
                                 reason=f"CTE {rel.name} 无法解析")]
        if column not in sub_scope.outputs:
            return [ColumnSource(
                kind="unknown", name=rel.name, alias=rel.alias, column=column,
                relation=rel,
                reason=f"CTE {rel.name} 的输出中不存在列 {column}")]
        return [ColumnSource(kind="cte", name=rel.name, alias=rel.alias,
                             column=column, relation=rel)]
    if rel.kind == "subquery" and rel.subquery is not None:
        sub_scope = _build_scope(
            rel.subquery, f"子查询 {rel.alias}", node, nodes_by_name,
            conn, schema, cte_scopes, parent=parent_scope)
        if column not in sub_scope.outputs:
            return [ColumnSource(
                kind="unknown", alias=rel.alias, column=column, relation=rel,
                reason=f"子查询 {rel.alias} 的输出中不存在列 {column}")]
        return [ColumnSource(kind="subquery", name=rel.alias, alias=rel.alias,
                             column=column, relation=rel)]
    return [ColumnSource(kind="unknown", alias=rel.alias, column=column,
                         relation=rel, reason="未知关系类型")]


# ---------------------------------------------------------------------------
# 节点级解析（含 CTE 作用域，每个节点只 parse 一次）
# ---------------------------------------------------------------------------
def _build_node_scope(
    node: NodeContext,
    nodes_by_name: dict[str, NodeContext],
    conn,
    schema: str,
) -> Scope:
    try:
        parsed = parse(node.sql_text)
    except Exception as exc:
        return Scope(label=node.model_name,
                     unsupported_reason=f"冻结 SQL 解析失败：{exc}",
                     owner_node=node)

    cte_scopes: dict[str, Scope] = {}
    for cte_name, cte_query in parsed.ctes.items():
        cte_scope = _build_scope(
            cte_query, f"CTE {cte_name}", node, nodes_by_name, conn,
            schema, cte_scopes)
        _apply_cte_column_aliases(cte_scope, cte_query, cte_name)
        cte_scopes[cte_name] = cte_scope
    return _build_scope(parsed, node.model_name, node, nodes_by_name, conn,
                        schema, cte_scopes)


def _apply_cte_column_aliases(scope: Scope, cte_query: ParsedQuery,
                              cte_name: str) -> None:
    """应用 WITH c(alias1, alias2, ...) 的列别名：按位置重命名 CTE 输出列。

    来源（ColumnSource 列表）保持不变，仅改变对外可见的输出列名，使外层
    引用新别名时能命中同一批内部来源。
    """
    aliases = cte_query.cte_col_aliases.get("__self__")
    if not aliases:
        return
    original_names = list(scope.outputs.keys())
    renamed: dict[str, list[ColumnSource]] = {}
    for new_name, old_name in zip(aliases, original_names):
        renamed[new_name.lower()] = scope.outputs[old_name]
    # 别名数量少于内部列时，剩余内部列保留原名
    for old_name in original_names[len(aliases):]:
        renamed[old_name] = scope.outputs[old_name]
    scope.outputs = renamed


# ---------------------------------------------------------------------------
# 血缘路径组装：从目标列递归追到 sales/refunds 字段或断点
# ---------------------------------------------------------------------------
def trace_column(run_id: int, node_id: int, column: str) -> dict:
    column = (column or "").strip().lower()
    with dbmod.conn_ctx() as conn:
        run = _load_run(conn, run_id)
        nodes = _load_nodes(conn, run_id)
        if not any(n.node_id == node_id for n in nodes):
            raise LineageError(f"试跑 #{run_id} 中不存在节点 #{node_id}")
        nodes_by_name = {n.model_name.lower(): n for n in nodes}
        schema = run["snapshot_schema"]

        target_node = next(n for n in nodes if n.node_id == node_id)
        result_columns = []
        if target_node.result_table:
            try:
                result_columns = _table_columns(conn, target_node.result_table)
            except Exception:
                result_columns = []
        if column and column not in result_columns:
            raise LineageError(
                f"节点 {target_node.model_name} 的结果列中不存在 {column}；"
                f"可用列: {', '.join(result_columns) or '（无）'}")

        root_scope = _build_node_scope(target_node, nodes_by_name, conn, schema)
        edges: list[dict] = []
        leaves: list[dict] = []
        blocked: list[dict] = []
        _walk_scope(root_scope, column, depth=0,
                    nodes=nodes, nodes_by_name=nodes_by_name, conn=conn,
                    schema=schema, edges=edges, leaves=leaves, blocked=blocked,
                    visiting=set())
        return {
            "run_id": run["id"],
            "snapshot_schema": schema,
            "node_id": node_id,
            "model_name": target_node.model_name,
            "target_column": column,
            "node_status": target_node.status,
            "result_columns": result_columns,
            "frozen_sql": target_node.sql_text,
            "edges": edges,
            "source_fields": leaves,
            "blocked": blocked,
            "complete": not blocked,
        }


def _walk_scope(scope: Scope, column: str, depth: int,
                nodes: list[NodeContext], nodes_by_name: dict[str, NodeContext],
                conn, schema: str, edges: list[dict], leaves: list[dict],
                blocked: list[dict], visiting: set[int]) -> None:
    node = scope.owner_node
    direct = scope.outputs.get(column)
    if direct is None:
        reason = scope.unsupported_reason or (
            f"{scope.label} 的冻结 SQL 中无法确定输出列 {column} 的表达式")
        blocked.append(_blocked(node, column, reason, depth))
        return
    for src in direct:
        _emit(scope, src, column, depth, nodes, nodes_by_name, conn, schema,
              edges, leaves, blocked, visiting)


def _emit(scope: Scope, src: ColumnSource, column: str, depth: int,
          nodes: list[NodeContext], nodes_by_name: dict[str, NodeContext],
          conn, schema: str, edges: list[dict], leaves: list[dict],
          blocked: list[dict], visiting: set[int]) -> None:
    node = scope.owner_node
    base_edge = {
        "depth": depth,
        "from_model": node.model_name,
        "from_node_id": node.node_id,
        "from_column": column,
        "via_alias": src.alias,
        "relation": src.name,
        "relation_kind": src.kind,
        "transform": src.transform,
    }

    if src.kind == "source":
        edges.append({**base_edge, "resolved": True, "target_type": "source_field",
                      "target": f"{src.name}.{src.column}",
                      "target_table": src.name, "target_column": src.column})
        leaf = {"table": src.name, "column": src.column}
        if leaf not in leaves:
            leaves.append(leaf)
        return

    if src.kind == "model":
        up_node = nodes_by_name.get((src.name or "").lower())
        if up_node is None:
            blocked.append(_blocked(node, column,
                                    f"上游模型 {src.name} 不在本批次冻结节点中", depth))
            return
        edges.append({**base_edge, "resolved": True, "target_type": "model_column",
                      "target": f"{up_node.model_name}.{src.column}",
                      "target_node_id": up_node.node_id,
                      "target_column": src.column})
        if up_node.node_id in visiting:
            blocked.append(_blocked(up_node, src.column,
                                    "依赖链出现循环，停止递归", depth + 1))
            return
        if up_node.status != "succeeded" or not up_node.result_table:
            blocked.append(_blocked(
                up_node, src.column,
                f"上游模型 {up_node.model_name} 状态为 {up_node.status}，"
                "没有成功结果表可继续追溯", depth + 1))
            return
        up_scope = _build_node_scope(up_node, nodes_by_name, conn, schema)
        _walk_scope(up_scope, src.column, depth + 1, nodes, nodes_by_name,
                    conn, schema, edges, leaves, blocked,
                    visiting | {node.node_id})
        return

    if src.kind in ("cte", "subquery"):
        edges.append({**base_edge, "resolved": True, "target_type": src.kind,
                      "target": f"{src.name or src.alias}.{src.column}",
                      "target_column": src.column})
        inner_scope = _inner_scope(scope, src, nodes_by_name, conn, schema)
        if inner_scope is None:
            blocked.append(_blocked(
                node, column,
                f"{src.kind} {src.name or src.alias} 无法解析", depth + 1))
            return
        _walk_scope(inner_scope, src.column, depth + 1, nodes, nodes_by_name,
                    conn, schema, edges, leaves, blocked, visiting)
        return

    if src.kind == "literal":
        blocked.append(_blocked(node, column,
                                src.reason or "常量/字面量表达式，无源字段", depth))
        return

    # ambiguous / unknown
    blocked.append(_blocked(node, column,
                            src.reason or f"无法确定列 {column} 的来源", depth))


def _inner_scope(scope: Scope, src: ColumnSource,
                 nodes_by_name: dict[str, NodeContext], conn,
                 schema: str) -> Scope | None:
    rel = src.relation
    if rel is None:
        return None
    node = scope.owner_node
    if rel.kind == "cte":
        return scope.cte_scopes.get(rel.name)
    if rel.kind == "subquery" and rel.subquery is not None:
        return _build_scope(
            rel.subquery, f"子查询 {rel.alias}", node, nodes_by_name, conn,
            schema, scope.cte_scopes)
    return None


def _blocked(node: NodeContext | None, column: str, reason: str, depth: int) -> dict:
    return {
        "depth": depth,
        "model": node.model_name if node else None,
        "node_id": node.node_id if node else None,
        "column": column,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# 批次可追溯列清单（供前端下拉）
# ---------------------------------------------------------------------------
def list_traceable_columns(run_id: int) -> dict:
    with dbmod.conn_ctx() as conn:
        run = _load_run(conn, run_id)
        nodes = _load_nodes(conn, run_id)
        out_nodes = []
        for n in nodes:
            cols: list[str] = []
            if n.status == "succeeded" and n.result_table:
                try:
                    cols = _table_columns(conn, n.result_table)
                except Exception:
                    cols = []
            out_nodes.append({
                "node_id": n.node_id,
                "model_name": n.model_name,
                "status": n.status,
                "columns": cols,
            })
        return {"run_id": run_id, "snapshot_schema": run["snapshot_schema"],
                "nodes": out_nodes}
