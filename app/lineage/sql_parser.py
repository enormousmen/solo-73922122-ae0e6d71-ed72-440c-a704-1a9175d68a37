"""列级血缘用的 SQL 解析器（只解析、不执行）。

设计目标
--------
平台模型只允许单条 ``SELECT`` / ``WITH ... SELECT``，本解析器在该子集内做
**保守**的静态分析，为列级血缘提供：

1. 顶层 SELECT 的输出列（含 ``*`` 展开标记、别名、隐式列名）；
2. 每个 FROM/JOIN 关系（物理表、子查询、CTE）及其别名作用域；
3. 输出表达式里引用到的列（限定名 ``别名.列`` 与裸列名）、嵌套子查询、
   聚合函数、窗口函数等信息。

严格的“不猜测”原则
-------------------
凡是无法在本解析器支持范围内确定来源的结构，都通过 ``unsupported_reason`` /
解析异常如实上报，由上层血缘服务生成“无法确定”的断点，绝不编造来源：

* 集合操作（UNION/INTERSECT/EXCEPT）；
* 窗口函数（``OVER``）；
* 嵌套 CTE（需求只保证单层 CTE）；
* ``t.*``（应用守卫本已禁止，双保险）；
* 任何语法解析失败。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 词法
# ---------------------------------------------------------------------------
KEYWORDS = {
    "with", "as", "select", "from", "where", "group", "by", "having",
    "order", "limit", "offset", "join", "left", "right", "full", "inner",
    "outer", "cross", "on", "using", "union", "intersect", "except",
    "all", "distinct", "case", "when", "then", "else", "end", "and", "or",
    "not", "is", "null", "in", "between", "like", "ilike", "over",
    "partition", "asc", "desc", "true", "false", "cast", "interval",
    "natural", "lateral", "filter", "within",
}
AGGREGATE_FUNCS = {
    "sum", "count", "avg", "min", "max", "string_agg", "array_agg",
    "bool_and", "bool_or", "every", "json_agg", "jsonb_agg",
    "json_object_agg", "jsonb_object_agg", "stddev", "stddev_pop",
    "stddev_samp", "variance", "var_pop", "var_samp", "bit_and", "bit_or",
    "percentile_cont", "percentile_disc",
}
MULTI_OPS = ("::", "<=", ">=", "<>", "!=", "||", "->>", "->")
SINGLE_PUNCT = set("(),.*+-/=%<>;")


@dataclass
class Tok:
    kind: str          # kw / id / punct / op / num / str
    value: str
    start: int
    end: int


class LexError(ValueError):
    pass


def tokenize(sql: str) -> list[Tok]:
    toks: list[Tok] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if sql.startswith("/*", i):  # PG 块注释可嵌套
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise LexError("块注释没有闭合")
            i = j
            continue
        start = i
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            else:
                raise LexError("字符串字面量没有闭合")
            toks.append(Tok("str", sql[start:j + 1], start, j + 1))
            i = j + 1
            continue
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            else:
                raise LexError("双引号标识符没有闭合")
            toks.append(Tok("id", sql[start + 1:j].replace('""', '"'), start, j + 1))
            i = j + 1
            continue
        if ch.isdigit():
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] in "._"):
                j += 1
            toks.append(Tok("num", sql[start:j], start, j))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            word = sql[start:j]
            kind = "kw" if word.lower() in KEYWORDS else "id"
            toks.append(Tok(kind, word, start, j))
            i = j
            continue
        two = sql[i:i + 2]
        if two in MULTI_OPS:
            toks.append(Tok("op", two, start, start + 2))
            i += 2
            continue
        if ch in SINGLE_PUNCT:
            toks.append(Tok("punct", ch, start, start + 1))
            i += 1
            continue
        raise LexError(f"无法识别的字符: {ch!r}")
    return toks


# ---------------------------------------------------------------------------
# 语法数据结构
# ---------------------------------------------------------------------------
@dataclass
class Ref:
    """表达式中的一个列引用。alias 为 None 表示裸列名。"""
    alias: str | None
    column: str
    pos: int


@dataclass
class SelectItem:
    expr_text: str
    alias: str | None
    implicit_name: str | None
    refs: list[Ref] = field(default_factory=list)
    subqueries: list["ParsedQuery"] = field(default_factory=list)
    funcs: list[str] = field(default_factory=list)
    has_window: bool = False
    is_star: bool = False
    star_alias: str | None = None          # t.* 中的 t（None 表示裸 *）
    has_aggregate: bool = False


@dataclass
class Relation:
    kind: str                              # table / subquery / cte
    name: str | None                       # 物理表名或 CTE 名
    alias: str
    subquery: "ParsedQuery | None" = None
    join_type: str = "from"                # from/inner/left/right/full/cross/cross_comma
    on_expr: str | None = None
    using_cols: list[str] = field(default_factory=list)


@dataclass
class ParsedQuery:
    items: list[SelectItem] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    ctes: dict[str, "ParsedQuery"] = field(default_factory=dict)
    cte_col_aliases: dict[str, list[str]] = field(default_factory=dict)
    has_setop: bool = False
    unsupported_reason: str | None = None
    raw_sql: str = ""

    def output_names(self) -> list[str | None]:
        """解析期可确定的输出名；星号项返回 None（需服务层按作用域展开）。"""
        names: list[str | None] = []
        for it in self.items:
            if it.is_star:
                names.append(None)
            elif it.alias:
                names.append(it.alias.lower())
            else:
                names.append(it.implicit_name.lower() if it.implicit_name else None)
        return names


class ParseError(ValueError):
    pass


def _matching_paren(cur: _Cursor, open_idx: int) -> int:
    """返回与 open_idx 处 '(' 配对的 ')' token 下标（忽略字符串/注释已在词法层处理）。"""
    depth = 0
    for j in range(open_idx, len(cur.toks)):
        tk = cur.toks[j]
        if tk.kind == "punct" and tk.value == "(":
            depth += 1
        elif tk.kind == "punct" and tk.value == ")":
            depth -= 1
            if depth == 0:
                return j
    raise ParseError("括号没有闭合")


# ---------------------------------------------------------------------------
# 递归下降解析
# ---------------------------------------------------------------------------
_CLAUSE_KW = {"where", "group", "having", "order", "limit", "offset",
              "union", "intersect", "except", "window"}
_JOIN_KW = {"join", "left", "right", "full", "inner", "cross", "natural"}


class _Cursor:
    def __init__(self, toks: list[Tok], sql: str):
        self.toks = toks
        self.sql = sql
        self.i = 0

    def peek(self, k: int = 0) -> Tok | None:
        j = self.i + k
        if j < 0 or j >= len(self.toks):
            return None
        return self.toks[j]

    def next(self) -> Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def eof(self) -> bool:
        return self.i >= len(self.toks)

    def is_kw(self, *names: str, k: int = 0) -> bool:
        t = self.peek(k)
        return t is not None and t.kind == "kw" and t.value.lower() in names

    def is_punct(self, p: str, k: int = 0) -> bool:
        t = self.peek(k)
        return t is not None and t.kind == "punct" and t.value == p

    def accept_kw(self, *names: str) -> Tok | None:
        if self.is_kw(*names):
            return self.next()
        return None

    def accept_punct(self, p: str) -> Tok | None:
        if self.is_punct(p):
            return self.next()
        return None

    def expect_punct(self, p: str) -> Tok:
        if not self.accept_punct(p):
            got = self.peek()
            raise ParseError(
                f"语法错误：期望 {p!r}，实际得到 {got.value if got else 'EOF'!r}"
            )
        return self.peek(0) or Tok("kw", "", 0, 0)

    def expect_id(self) -> Tok:
        t = self.peek()
        if t is None or t.kind != "id":
            raise ParseError(f"语法错误：期望标识符，实际得到 {t.value if t else 'EOF'!r}")
        return self.next()

    def text(self, start_tok: Tok, end_tok: Tok) -> str:
        return self.sql[start_tok.start:end_tok.end].strip()


def parse(sql: str) -> ParsedQuery:
    """解析一条模型 SQL，失败时抛 ParseError/LexError。"""
    if not sql or not sql.strip():
        raise ParseError("SQL 为空")
    toks = tokenize(sql)
    if not toks:
        raise ParseError("SQL 为空")
    if any(t.kind == "punct" and t.value == "(" for t in toks):
        depth = 0
        for t in toks:
            if t.kind == "punct" and t.value == "(":
                depth += 1
            elif t.kind == "punct" and t.value == ")":
                depth -= 1
            if depth < 0:
                raise ParseError("括号不匹配：存在多余的右括号")
        if depth != 0:
            raise ParseError("括号不匹配：存在未闭合的左括号")
    cur = _Cursor(toks, sql)
    q = _parse_query(cur, inside_cte=False)
    if not cur.eof():
        t = cur.peek()
        raise ParseError(f"语法错误：语句结束后仍有多余标记 {t.value!r}")
    if not q.items:
        raise ParseError("SELECT 子句至少需要一个输出列")
    q.raw_sql = sql.strip().rstrip(";").strip()
    return q


def _parse_query(cur: _Cursor, inside_cte: bool,
                 limit_idx: int | None = None) -> ParsedQuery:
    q = ParsedQuery(raw_sql=cur.sql)
    if cur.accept_kw("with"):
        _parse_ctes(cur, q, inside_cte, limit_idx)
    _parse_select_body(cur, q, limit_idx=limit_idx)
    return q


def _parse_bounded_query(cur: _Cursor, close_idx: int) -> ParsedQuery:
    """解析一个限定在 close_idx（闭合括号）之前的子查询。"""
    return _parse_query(cur, inside_cte=True, limit_idx=close_idx)


def _parse_ctes(cur: _Cursor, q: ParsedQuery, inside_cte: bool,
                hard_limit: int | None) -> None:
    if inside_cte and q.unsupported_reason is None:
        # 需求只保证单层 CTE：嵌套 WITH 无法按既定口径确定，标记后仍尽量解析
        q.unsupported_reason = "存在嵌套 CTE（WITH 内再定义 WITH），超出支持范围"
    while True:
        if hard_limit is not None and cur.i >= hard_limit:
            return
        name_tok = cur.expect_id()
        cte_name = name_tok.value.lower()
        col_aliases: list[str] = []
        if cur.accept_punct("("):
            while not cur.is_punct(")"):
                col_aliases.append(cur.expect_id().value.lower())
                if not cur.accept_punct(","):
                    break
            cur.expect_punct(")")
        if not cur.accept_kw("as"):
            raise ParseError("CTE 定义缺少 AS")
        cur.expect_punct("(")
        close_idx = _matching_paren(cur, cur.i - 1)
        if hard_limit is not None and close_idx > hard_limit:
            raise ParseError("CTE 括号越过外层边界")
        sub = _parse_query(cur, inside_cte=True, limit_idx=close_idx)
        if cur.peek() is None or not cur.is_punct(")"):
            raise ParseError("CTE 定义缺少闭合括号")
        cur.i = close_idx + 1  # 内部解析严格停在闭合括号之前
        if sub.unsupported_reason and not q.unsupported_reason:
            q.unsupported_reason = sub.unsupported_reason
        if col_aliases:
            sub.cte_col_aliases["__self__"] = col_aliases
        q.ctes[cte_name] = sub
        q.cte_col_aliases[cte_name] = col_aliases
        if not cur.accept_punct(","):
            break


def _parse_select_body(cur: _Cursor, q: ParsedQuery, limit_idx: int | None = None) -> None:
    if not cur.accept_kw("select"):
        t = cur.peek()
        raise ParseError(f"语法错误：期望 SELECT，实际得到 {t.value if t else 'EOF'!r}")
    cur.accept_kw("distinct")
    cur.accept_kw("all")
    _parse_select_list(cur, q)
    if cur.accept_kw("from"):
        _parse_from(cur, q)
    _consume_optional_clauses(cur, limit_idx)
    # 集合操作：解析但标记为不支持（不在需求口径内，不猜测合并分支来源）
    if cur.accept_kw("union", "intersect", "except"):
        cur.accept_kw("all")
        cur.accept_kw("distinct")
        other = ParsedQuery(raw_sql=cur.sql)
        _parse_select_body(cur, other, limit_idx=limit_idx)
        q.has_setop = True
        if not q.unsupported_reason:
            q.unsupported_reason = (
                "包含 UNION/INTERSECT/EXCEPT 集合操作，输出列可能来自多个分支，"
                "无法唯一确定来源")
        q.relations.extend(other.relations)
    if any(it.has_window for it in q.items) or _subquery_has_window(q):
        if not q.unsupported_reason:
            q.unsupported_reason = "包含窗口函数（OVER），窗口口径无法用普通列血缘表达"
    # 部分 SELECT 项在 analyze 阶段已标记窗口，这里再兜底扫描 OVER 关键字
    if not q.unsupported_reason and _raw_has_over(cur):
        q.unsupported_reason = "包含窗口函数（OVER），窗口口径无法用普通列血缘表达"


def _raw_has_over(cur: _Cursor) -> bool:
    return any(t.kind == "kw" and t.value.lower() == "over" for t in cur.toks)


def _subquery_has_window(q: ParsedQuery) -> bool:
    for it in q.items:
        for sub in it.subqueries:
            if any(s.has_window for s in sub.items) or _subquery_has_window(sub):
                return True
    for rel in q.relations:
        if rel.subquery and (
            any(it.has_window for it in rel.subquery.items)
            or _subquery_has_window(rel.subquery)
        ):
            return True
    return False


def _parse_select_list(cur: _Cursor, q: ParsedQuery) -> None:
    while True:
        start = cur.peek()
        if start is None:
            raise ParseError("语法错误：SELECT 后缺少输出列")
        # 裸 * 或 t.*
        if cur.is_punct("*"):
            star = cur.next()
            q.items.append(SelectItem(
                expr_text="*", alias=None, implicit_name=None,
                is_star=True, star_alias=None))
        elif cur.peek(1) and cur.is_punct(".", k=1) and cur.is_punct("*", k=2):
            alias_tok = cur.next()
            cur.next()  # .
            cur.next()  # *
            q.items.append(SelectItem(
                expr_text=f"{alias_tok.value}.*", alias=None, implicit_name=None,
                is_star=True, star_alias=alias_tok.value.lower()))
        else:
            item, end = _parse_select_item(cur)
            q.items.append(item)
        # 非星号项后面可能带着 [AS] alias，统一在此消费并写回 item
        if not q.items[-1].is_star:
            cur.accept_kw("as")
            alias_tok = cur.peek()
            if alias_tok and alias_tok.kind == "id":
                alias_name = cur.next().value
                q.items[-1].alias = alias_name.lower()
                q.items[-1].implicit_name = None
        if not cur.accept_punct(","):
            break


def _at_select_item_boundary(cur: _Cursor, depth: int) -> bool:
    t = cur.peek()
    if t is None:
        return True
    if depth > 0:
        # 括号未闭合前，任何 token 都只是表达式内容
        return False
    if t.kind == "punct" and t.value in (",", ")"):
        return True
    if t.kind == "kw":
        w = t.value.lower()
        if w in ("as", "from", "where", "group", "having", "order", "limit",
                 "offset", "union", "intersect", "except", "window"):
            return True
        if w in ("left", "right", "full", "inner", "cross", "natural", "join"):
            return True
    # 裸标识符别名：id 本身是一个“独立 token”，前一个 token 为表达式结尾
    # （标识符/字面量/闭括号，且不是 '.'），后一个 token 是子句边界。
    # 这样 a.store_id 中的 store_id（前为 '.'）不会被误判为别名。
    if t.kind == "id":
        prev_tok = cur.peek(-1)
        nxt = cur.peek(1)
        prev_ends_expr = prev_tok is not None and (
            prev_tok.kind in ("id", "num", "str")
            or prev_tok.kind == "punct" and prev_tok.value == ")")
        nxt_is_boundary = nxt is not None and (
            nxt.kind == "punct" and nxt.value in (",", ")")
            or nxt.kind == "kw" and nxt.value.lower() in (
                "as", "from", "where", "group", "having", "order", "limit",
                "offset", "union", "intersect", "except", "window",
                "left", "right", "full", "inner", "cross", "natural", "join"))
        if prev_ends_expr and nxt_is_boundary:
            return True
    return False


def _parse_select_item(cur: _Cursor) -> tuple[SelectItem, Tok]:
    start_idx = cur.i
    start = cur.peek()
    depth = 0
    end = start
    end_idx = cur.i
    subqueries: list[ParsedQuery] = []
    # 子查询覆盖的 token 下标区间 [open, close+1)，供表达式分析整体跳过
    sub_token_ranges: list[tuple[int, int]] = []
    while not _at_select_item_boundary(cur, depth):
        t = cur.peek()
        # depth=0 的 AS 是“输出列别名”起点，不属于表达式本体
        if depth == 0 and t.kind == "kw" and t.value.lower() == "as":
            break
        t = cur.next()
        if t.kind == "punct" and t.value == "(":
            if cur.is_kw("select") or cur.is_kw("with"):
                open_idx = cur.i - 1
                close_idx = _matching_paren(cur, open_idx)
                sub = _parse_bounded_query(cur, close_idx)
                subqueries.append(sub)
                cur.i = close_idx + 1
                sub_token_ranges.append((open_idx, close_idx + 1))
                end = cur.peek() or end
                end_idx = cur.i
                continue
            depth += 1
        elif t.kind == "punct" and t.value == ")":
            depth -= 1
        end = t
        end_idx = cur.i
    expr_text = cur.text(start, end)
    refs, funcs, has_window = analyze_expression(
        cur.toks, start_idx, end_idx, sub_token_ranges)
    # 别名由 _parse_select_list 统一消费（裸标识符别名与 AS 别名同一入口）
    alias = None
    implicit = None
    if len(refs) == 1 and not funcs and not subqueries:
        implicit = refs[0].column
    elif funcs and not subqueries:
        # 无别名的函数表达式：PostgreSQL 以最外层函数名作为结果列名
        implicit = funcs[0].lower()
    item = SelectItem(
        expr_text=expr_text, alias=None,
        implicit_name=implicit, refs=refs, subqueries=subqueries,
        funcs=funcs, has_window=has_window,
        has_aggregate=any(f.lower() in AGGREGATE_FUNCS for f in funcs))
    return item, end


def _take_alias(cur: _Cursor) -> str | None:
    """SELECT 项之后的 [AS] alias。"""
    t = cur.peek()
    if t is None:
        return None
    if t.kind == "kw" and t.value.lower() == "as":
        cur.next()
        return cur.expect_id().value
    if t.kind == "id":
        # 裸标识符作别名（PG 允许省略 AS）
        return cur.next().value
    return None


def _parse_from(cur: _Cursor, q: ParsedQuery) -> None:
    first = _parse_factor(cur, q)
    _attach_join_clause(cur, first, "from")
    q.relations.append(first)
    while True:
        t = cur.peek()
        if t is None:
            return
        if t.kind == "punct" and t.value == ",":
            cur.next()
            rel = _parse_factor(cur, q)
            _attach_join_clause(cur, rel, "cross_comma")
            q.relations.append(rel)
            continue
        if t.kind == "kw" and t.value.lower() in (
                "where", "group", "having", "order", "limit", "offset",
                "union", "intersect", "except", "window"):
            return
        join_type = _take_join_keywords(cur)
        if join_type is None:
            return
        rel = _parse_factor(cur, q)
        _attach_join_clause(cur, rel, join_type)
        q.relations.append(rel)


def _attach_join_clause(cur: _Cursor, rel: Relation, join_type: str) -> None:
    rel.join_type = join_type
    if join_type in ("cross", "cross_comma"):
        return
    if cur.accept_kw("on"):
        rel.on_expr = _consume_predicate(cur)
    elif cur.accept_kw("using"):
        cur.expect_punct("(")
        cols: list[str] = []
        while not cur.is_punct(")"):
            cols.append(cur.expect_id().value.lower())
            if not cur.accept_punct(","):
                break
        cur.expect_punct(")")
        rel.using_cols = cols


def _take_join_keywords(cur: _Cursor) -> str | None:
    if not cur.peek() or cur.peek().kind != "kw":
        return None
    words: list[str] = []
    while cur.peek() and cur.peek().kind == "kw" and cur.peek().value.lower() in _JOIN_KW:
        words.append(cur.next().value.lower())
    if "join" not in words:
        if words:
            raise ParseError(f"语法错误：JOIN 关键字不完整: {' '.join(words)}")
        return None
    if "cross" in words:
        return "cross"
    if "left" in words:
        return "left"
    if "right" in words:
        return "right"
    if "full" in words:
        return "full"
    return "inner"


def _parse_factor(cur: _Cursor, q: ParsedQuery) -> Relation:
    t = cur.peek()
    if t is None:
        raise ParseError("语法错误：FROM 后缺少关系")
    if t.kind == "punct" and t.value == "(":
        if cur.is_kw("select", k=1) or cur.is_kw("with", k=1):
            cur.next()
            close_idx = _matching_paren(cur, cur.i - 1)
            sub = _parse_query(cur, inside_cte=False)
            if cur.peek() is None or not cur.is_punct(")"):
                raise ParseError("子查询缺少闭合括号")
            cur.i = close_idx + 1
            alias = _take_relation_alias(cur, required=True)
            return Relation(kind="subquery", name=None, alias=alias, subquery=sub)
        # 普通括号分组的 JOIN：展平处理
        cur.next()
        rel = _parse_factor(cur, q)
        cur.expect_punct(")")
        outer_alias = _take_relation_alias(cur, required=False)
        if outer_alias:
            rel.alias = outer_alias
        return rel
    name_tok = cur.expect_id()
    name = name_tok.value.lower()
    alias = _take_relation_alias(cur, required=False) or name
    if name in q.ctes:
        return Relation(kind="cte", name=name, alias=alias)
    return Relation(kind="table", name=name, alias=alias)


def _take_relation_alias(cur: _Cursor, required: bool) -> str | None:
    cur.accept_kw("as")
    t = cur.peek()
    if t and t.kind == "id":
        return cur.next().value.lower()
    if required:
        raise ParseError("语法错误：子查询必须指定别名")
    return None


def _consume_predicate(cur: _Cursor) -> str:
    start = cur.peek()
    depth = 0
    end = start
    while True:
        t = cur.peek()
        if t is None:
            break
        if depth == 0:
            if t.kind == "punct" and t.value in (",", ")"):
                break
            if t.kind == "kw" and (
                t.value.lower() in ("where", "group", "having", "order",
                                    "limit", "offset", "union", "intersect",
                                    "except", "window")
                or t.value.lower() in _JOIN_KW
            ):
                break
        if t.kind == "punct" and t.value == "(":
            depth += 1
        elif t.kind == "punct" and t.value == ")":
            if depth == 0:
                break
            depth -= 1
        cur.next()
        end = t
    return cur.text(start, end)


def _consume_optional_clauses(cur: _Cursor, limit_idx: int | None) -> None:
    # WHERE / GROUP BY / HAVING / ORDER BY / LIMIT / OFFSET 等子句不定义输出列
    # 血缘，消费到下一个同级关键字为止。limit_idx 为括号子查询/CTE 的闭合括号
    # token 下标：到达它必须停下，把闭合括号交还给调用方。
    depth = 0
    while not cur.eof() and (limit_idx is None or cur.i < limit_idx):
        t = cur.peek()
        if depth == 0:
            if t.kind == "kw" and t.value.lower() in (
                    "union", "intersect", "except", "window"):
                return
            if t.kind == "punct" and t.value == ")":
                return
        if t.kind == "punct" and t.value == "(":
            depth += 1
        elif t.kind == "punct" and t.value == ")":
            depth -= 1
        cur.next()


# ---------------------------------------------------------------------------
# 表达式分析：在 token 区间内抽取列引用、函数名、窗口标记
# ---------------------------------------------------------------------------
def analyze_expression(
    toks: list[Tok], start_idx: int, end_idx: int,
    sub_token_ranges: list[tuple[int, int]],
) -> tuple[list[Ref], list[str], bool]:
    """在 token 区间 [start_idx, end_idx) 内抽取列引用、函数名与窗口标记。

    ``sub_token_ranges`` 为区间内子查询（含外层括号）覆盖的 token 下标区间
    [open, close+1)，这些区间整体跳过，其内部列由对应的 ParsedQuery 单独分析。
    """
    refs: list[Ref] = []
    funcs: list[str] = []
    has_window = False

    def in_skip(idx: int) -> bool:
        return any(a <= idx < b for a, b in sub_token_ranges)

    # 区间内的普通括号（非子查询）配对，用于识别 CAST(... AS type)
    paren_pairs: dict[int, int] = {}  # 开括号下标 -> 闭括号下标
    stack: list[int] = []
    for k in range(start_idx, end_idx):
        if in_skip(k):
            continue
        t = toks[k]
        if t.kind == "punct" and t.value == "(":
            stack.append(k)
        elif t.kind == "punct" and t.value == ")" and stack:
            paren_pairs[stack.pop()] = k

    # CAST( ... AS type ) 中 AS 及其类型名的下标（CAST 是关键字 token）
    cast_skip: set[int] = set()
    for open_k, close_k in paren_pairs.items():
        if open_k > 0 and toks[open_k - 1].kind in ("id", "kw") \
                and toks[open_k - 1].value.lower() == "cast":
            for j in range(open_k + 1, close_k):
                if toks[j].kind == "kw" and toks[j].value.lower() == "as":
                    cast_skip.add(j)
                    if j + 1 < close_k:
                        cast_skip.add(j + 1)  # 类型名一并跳过

    i = start_idx
    while i < end_idx:
        if in_skip(i):
            i += 1
            continue
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        prev = toks[i - 1] if i > 0 else None

        # 窗口函数标记
        if t.kind == "kw" and t.value.lower() == "over":
            has_window = True
            i += 1
            continue
        # 函数名（关键字函数名同样识别，如 CAST(...)）
        if nxt and nxt.kind == "punct" and nxt.value == "(" and t.kind in ("id", "kw"):
            funcs.append(t.value.lower())
            i += 1
            continue
        # CAST(x AS type)：AS 及其类型名不是列
        if i in cast_skip:
            i += 1
            continue
        # INTERVAL '...'：跳过 INTERVAL 与其后字面量
        if t.kind == "kw" and t.value.lower() == "interval":
            i += 2
            continue
        # a::type：类型名不是列
        if prev and prev.kind == "op" and prev.value == "::":
            i += 1
            continue
        if t.kind == "op" and t.value == "::":
            i += 1
            continue
        if t.kind == "id":
            # 限定名 alias.column：先遇到 alias（后跟 .），再遇到 column
            if nxt and nxt.kind == "punct" and nxt.value == ".":
                i += 1
                continue
            if prev and prev.kind == "punct" and prev.value == ".":
                alias_tok = toks[i - 2] if i > 1 else None
                alias = (alias_tok.value.lower()
                         if alias_tok and alias_tok.kind == "id" else None)
                refs.append(Ref(alias=alias, column=t.value.lower(), pos=t.start))
                i += 1
                continue
            refs.append(Ref(alias=None, column=t.value.lower(), pos=t.start))
        i += 1

    return refs, funcs, has_window
