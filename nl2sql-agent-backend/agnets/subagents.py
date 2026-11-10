"""子智能体定义。

子智能体是主智能体通过 ``task`` 工具派活的对象：每个 spec 说明自己叫什么、什么时候该派给它、带哪些工具
与技能（见 deepagents 的 ``SubAgent``）。这里只放声明，装配在 :meth:`agnets.Agent.build`——主智能体派活时
子智能体会继承父级的状态（含 ``database_id``）与运行上下文，故工具在子智能体里能照常读库。

目前三个：

- 指标口径审核（``metric-caliber-reviewer``）：用 ``recall_metrics`` 查库，按技能
  ``skills/metric-caliber-review`` 核对口径——问题涉及哪些指标、候选里哪些对得上、用户的补充能不能当口径，
  最后用 ``commit_metrics`` 定稿；
- 表召回（``table-recaller``）：把问题拆成多条检索语句，用 ``recall_tables`` 并行召回并筛表，缺表就继续拆，
  最后用 ``commit_tables`` 把表与关联关系写回状态；
- SQL 生成与执行（``sql-writer``）：读候选表与问题，先把口径、连表、过滤与聚合想清楚再写 SQL
  （只读、逻辑正确优先，一次写对），确认无误后用 ``check_sql`` 过语法、``execute_sql`` 执行，
  结果写回 ``state.data``。

三者都不写系统提示词：流程、判据与 CoT 示例都在各自的技能里，要调整判断方式就改技能，不动这里的声明。
"""

from deepagents import SubAgent

from agnets.tools import check_sql, commit_metrics, commit_tables, execute_sql, read_sql_context, recall_metrics, \
    recall_tables

#: 技能在智能体虚拟文件系统中的挂载点。主图把 ``skills`` 目录挂到后端的这个路径上（见 agnets.Agent.build），
#: 子智能体按同一路径读技能；两处必须一致，故常量定义在这里、由主图引用
SKILLS_ROUTE = "/skills/"

#: 指标口径审核子智能体：召回候选 → 判断问题涉及哪些指标、哪些口径对得上 → 缺口径时反问用户 →
#: 定稿（commit_metrics 把命中的编号与用户补充的定义写回状态）
METRIC_CALIBER_REVIEWER: SubAgent = {
    "name": "metric-caliber-reviewer",
    "description": "核对业务指标口径时用它。**把用户的问题原句写进任务说明**（可选再带上系统上一轮的反问与用户的回答），"
                   "它会从会话状态里取问题去召回本业务库的指标，判断：问题涉及哪些指标口径、候选里哪些口径一致、"
                   "哪些没对上需要反问用户补定义；用户补了口径之后，它把命中的指标与补充的定义一起定稿写回状态"
                   "（state.metric_infos），并把问题里的指标名称换成口径描述、改写后写回 state.question。"
                   "它只回一句自然语言，且以标签开头：`需要澄清：<问句>`（转达给用户，本轮到此为止）或 "
                   "`已完成：<完成说明 + 改写后的问题原句>`（口径已写回状态，接着派 table-recaller）。"
                   "生成 SQL 之前确定「这个指标按什么口径算」时用它。",
    "tools": [recall_metrics, commit_metrics],
    "skills": [SKILLS_ROUTE],
}

#: 表召回子智能体：识别实体 → 拆成多条语句并行召回 → 筛相关表（缺表就继续拆） → 识别关联 → 定稿
TABLE_RECALLER: SubAgent = {
    "name": "table-recaller",
    "description": "找数据表时用它。**把用户的问题原句写进任务说明**，它会先认出问题涉及哪些实体（申请单、订单、"
                   "部门、员工……），按实体拆成多条自然语言语句并行召回数据表并筛出真正要用的表；缺表就继续拆、"
                   "再召回，直到不再缺；最后识别这些表之间的关联关系，把表与关系写回状态（state.final_table_infos，"
                   "字段里的 related_columns 也带上）。它只回一句自然语言，且以标签开头：`已定表：…`（接着派 "
                   "sql-writer）、`缺表：…`（还缺哪类表）、`无表：…`（一张都没召回到）。"
                   "生成 SQL 之前定位数据表时用它。",
    "tools": [recall_tables, commit_tables],
    "skills": [SKILLS_ROUTE],
}

#: SQL 生成与执行子智能体：读上下文 → 想清楚逻辑 → 写 SQL → 自查逻辑 → 语法校验（有上限） → 执行 → 结果写回状态
SQL_WRITER: SubAgent = {
    "name": "sql-writer",
    "description": "生成并执行 SQL 时用它。**把用户的问题原句写进任务说明**（前置的表与指标应该已经写回状态），"
                   "它会读取状态里的候选表与问题，先把口径、连表、过滤与聚合想清楚，再写一条只读 SQL，"
                   "写完逐条自查逻辑（是不是用户要的那个数），确信正确后才用 check_sql 做语法与库结构校验"
                   "（报错就改，最多 5 次），最后 execute_sql"
                   "执行并把结果集写回状态（state.data）。它只回一句自然语言，且以标签开头：`执行完成：<行数>`"
                   "（主智能体据此汇总答复用户）、`执行失败：…`（改到上限仍不通，如实告知用户）、"
                   "`缺少表：…`（状态里没有候选表，要回退去召回表）。表与指标都定好、要出 SQL 与结果时用它。",
    "tools": [read_sql_context, check_sql, execute_sql],
    "skills": [SKILLS_ROUTE],
}
