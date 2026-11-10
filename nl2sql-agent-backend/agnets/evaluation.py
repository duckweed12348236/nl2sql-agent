"""评估：出题（生成问题与参考 SQL、校验纠正、落库数据集）与评测（检索答题、SQL 等效性打分、解读报告落库）。

两步各是一个独立的工具函数，都是异步生成器、逐条产出进度帧（Step，见 agnets.schemas）：

- generate_dataset(database_id, table_ids, size, thread_id)：出题 → EXPLAIN 校验纠正 → 落库数据集，
  最后一帧带 dataset_id；
- evaluate_dataset(dataset_id, database_id)：评测已落库的数据集，结果挂在它下面。

不再有评估图与会话状态：两步之间要用的东西全在元数据库里（Dataset / DatasetItem），跑完一步就结束，
调用方（Celery 任务、evaluate.py 脚本）自己决定下一步跑什么。
"""

from asyncio import Semaphore, as_completed, create_task, gather
from dataclasses import asdict
from datetime import datetime
from typing import AsyncGenerator

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai.chat_models.base import BaseChatOpenAI
from loguru import logger
from openai import AsyncOpenAI
from tortoise.transactions import in_transaction

# 导入即给 ragas 打依赖补丁，必须早于任何 ragas 导入（见 agnets.compat 的说明）
import agnets.compat  # noqa: F401
from ragas.llms.base import llm_factory
from ragas.metrics.collections import SQLSemanticEquivalence

from agnets import RetrievalAgent
from models import explain_sql, get_column_values
from models.dataset import Dataset, DatasetItem, Result, ResultItem
from models.meta import Column, Table
from agnets.models import fetch_chat_model, invoke
from plugins.agents.prompts import EVALUATION_REVIEW_PROMPT, EVALUATION_SQL_CORRECTION_PROMPT, \
    EVALUATION_SQL_GENERATION_PROMPT, QUESTION_GENERATION_PROMPT
from agnets.schemas import DatasetItemInfo, EvalColumnInfo, EvalTableInfo, EvaluationReview, SqlList, Step, \
    StepStatus, StringList
from plugins.json import dump

# 每个字段取回的前若干条取值，供模型参考
VALUES_LIMIT = 5
# SQL 校验的批大小：按该数量为单位用 EXPLAIN 校验
SQL_BATCH_SIZE = 5
# 评测阶段的并发度：每个问题都要跑一整条检索图、再做一次指标判定，都是大模型请求，
# 并发过高会触发服务端限流，故与采集流程同样限制在个位数
EVALUATION_CONCURRENCY = 5
# 元数据库连接名（见 config.TORTOISE_ORM_CONFIG）：落库数据集（数据集与条目）在一个事务里完成
META_CONNECTION = "meta"


def build_sql_equivalence(llm: BaseChatOpenAI) -> SQLSemanticEquivalence:
    """按给定模型的连接信息装配 SQL 语义等效性指标，供评测使用。

    :param llm: 出题与生成 SQL 用的模型（agent 的默认模型）
    :return: ragas 的 SQL 语义等效性指标

    指标判定「两条 SQL 在同一数据库上执行是否得到相同结果」，分数 1.0 表示等效、0.0 表示不等效，
    判定理由由 ragas 写在 MetricResult.reason 里。

    ragas 的 collections 指标要求传入 instructor 包装的结构化输出客户端
    （InstructorBaseRagasLLM），与 agent 用的 langchain ChatOpenAI 不是同一套封装，无法直接复用，
    故这里从传入模型的连接信息（端点、密钥、模型名）另建一个 OpenAI 兼容客户端：
    指标与 agent 用的仍是同一个模型，默认模型换掉时指标随之一同切换。
    """
    client = AsyncOpenAI(
        api_key=llm.openai_api_key.get_secret_value(),
        base_url=llm.openai_api_base
    )
    return SQLSemanticEquivalence(llm=llm_factory(llm.model_name, client=client))


async def build_table_infos(table_ids: list[str], database_id: int) -> list[EvalTableInfo]:
    """按表编号取回库上下文：表描述、字段描述 / 别名 / 角色，以及每个字段的前 VALUES_LIMIT 条取值。

    :param table_ids: 出题 / 评测所用的表编号（元数据库 Table.id，形如 <库编号>.<表名>）
    :param database_id: 本次所用业务库编号（Database.id）：字段取值在该库上取（见 models.get_connection）
    :return: 表信息快照，顺序与传入的编号一致；元数据库里已找不到的表自动略过

    出题用它组装出题所用的表，评测在开跑前用它重新取一遍（评测之间元数据可能被改过或重新采集过）。
    """
    ids = list(dict.fromkeys(table_ids))
    tables = await Table.filter(id__in=ids).all()
    if not tables:
        logger.warning(f"build_table_infos: none of {len(ids)} table id(s) exists")
        return []
    columns = await Column.filter(table_id__in=[x.id for x in tables]).all()
    grouped = {}
    for column in columns:
        grouped.setdefault(column.table_id, []).append(column)
    # column.table_id 是表编号，未必等于业务表名，取值查询需按真实表名
    table_names = {table.id: table.name for table in tables}
    # 同一次查询取回每个字段的前 VALUES_LIMIT 条取值，按「表名.列名」归组（供模型理解字段的业务含义）
    values = await get_column_values(
        [(table_names[column.table_id], column.name, VALUES_LIMIT, 0) for column in columns], database_id
    )
    infos = {
        table.id: EvalTableInfo(
            id=table.id,
            name=table.name,
            role=table.role,
            description=table.description,
            columns=[
                EvalColumnInfo(
                    id=column.id,
                    name=column.name,
                    type=column.type,
                    description=column.description,
                    alias=column.alias,
                    table_id=column.table_id,
                    role=column.role,
                    values=values.get(f"{table_names[column.table_id]}.{column.name}", []),
                )
                for column in grouped.get(table.id, [])
            ],
        )
        for table in tables
    }
    logger.info(f"build_table_infos: built {len(infos)} of {len(ids)} table(s)")
    # 按传入顺序归位，保持上下文顺序稳定（表已被删掉的自然从上下文里去掉）
    return [infos[table_id] for table_id in ids if table_id in infos]


def render_schema(table_infos: list[EvalTableInfo]) -> str:
    """把出题所用的表结构渲染成文本，作为 SQL 等效性判定的数据库上下文。

    :param table_infos: 出题与生成 SQL 时使用的表及其字段
    :return: 逐表逐字段的描述文本
    """
    lines = []
    for table in table_infos:
        lines.append(f"表 {table.name}（{table.role}）：{table.description or ''}".rstrip())
        for column in table.columns or []:
            description = f"{column.name} {column.type or ''}".strip()
            if column.description:
                description = f"{description}：{column.description}"
            lines.append(f"  - {description}")
    return "\n".join(lines)


async def generate_dataset(database_id: int, table_ids: list[str], size: int,
                           thread_id: int) -> AsyncGenerator[Step, None]:
    """出题并落库数据集：生成问题 → 按问题生成 SQL → EXPLAIN 校验纠正 → 数据集落库。

    :param database_id: 出题所用的业务库编号（Database.id）：表信息与 SQL 校验都连它
    :param table_ids: 出题所用的表编号（元数据库 Table.id，形如 <库编号>.<表名>）
    :param size: 生成多少个问题
    :param thread_id: 本轮会话编号（雪花 ID）：记在数据集上（见 models.dataset.Dataset.thread_id）
    :return: 逐条进度帧；数据集落库那一帧起带上 dataset_id，失败帧带原因

    表是出题的必要条件，没有表（或指定的表都已不存在）则以失败帧收尾、不落库；问题按字符串数组校验，
    SQL 按带整数 key 的 {key, sql} 列表校验，再按 key 与问题配对。校验不通过的 SQL 交给模型纠正一次，
    随后把最终的问题与参考 SQL 落库（一条 Dataset 加逐条 DatasetItem），库上下文一并存进
    Dataset.reference_contexts，评测时据此还原出题所用的表。
    """
    if not table_ids:
        logger.warning("generate_dataset: no table given")
        error = "没有可用的表，无法生成问题"
        yield Step(message=error, status=StepStatus.FAILED, error=error)
        return

    # 出题用的表信息在这里组装：取的是元数据库当前的一份
    table_infos = await build_table_infos(table_ids, database_id)
    if not table_infos:
        logger.warning("generate_dataset: none of the specified table exists")
        error = "指定的表已不存在，无法生成问题"
        yield Step(message=error, status=StepStatus.FAILED, error=error)
        return

    yield Step(message=f"正在生成 {size} 个问题")
    tables = [asdict(x) for x in table_infos]
    payload = {"n": size, "tables": tables}
    # 对话模型按 model 表里的默认登记取（实例有进程内缓存）
    model = await fetch_chat_model()
    # 模型输出不一定是合法字符串数组，重试后仍不合规则终止
    questions, error = await invoke(model, [
        SystemMessage(content=QUESTION_GENERATION_PROMPT),
        HumanMessage(content=dump(payload, default=str)),
    ], StringList)
    if error:
        logger.error(error)
        yield Step(message=f"问题生成失败：{error}", status=StepStatus.FAILED, error=error)
        return
    logger.info(f"generate_dataset: generated {len(questions)} question(s)")
    yield Step(message=f"问题生成完成：共 {len(questions)} 个", status=StepStatus.SUCCESS)

    # 基于问题与表继续生成 SQL：为每个问题编号（key），结果按 key 对应返回
    yield Step(message="正在生成 SQL")
    payload = {
        "questions": [{"key": index, "question": question}
                      for index, question in enumerate(questions, start=1)],
        "tables": tables,
    }
    records, error = await invoke(model, [
        SystemMessage(content=EVALUATION_SQL_GENERATION_PROMPT),
        HumanMessage(content=dump(payload, default=str)),
    ], SqlList)
    if error:
        logger.error(error)
        yield Step(message=f"SQL 生成失败：{error}", status=StepStatus.FAILED, error=error)
        return
    logger.info(f"generate_dataset: generated {len(records)} SQL for {len(questions)} question(s)")
    yield Step(message=f"SQL 生成完成：共 {len(records)} 条", status=StepStatus.SUCCESS)

    # 按 key 把 SQL 配回对应问题，组装成数据集条目
    sql_by_key = {record.key: record.sql for record in records}
    missing = [index for index in range(1, len(questions) + 1) if index not in sql_by_key]
    if missing:
        logger.warning(f"generate_dataset: missing SQL for key(s): {missing}")
    dataset_info = [
        DatasetItemInfo(question=question, sql=sql_by_key[index])
        for index, question in enumerate(questions, start=1)
        if index in sql_by_key
    ]
    if not dataset_info:
        logger.warning("generate_dataset: no question matched a generated SQL")
        error = "模型没有为任何问题生成 SQL"
        yield Step(message=error, status=StepStatus.FAILED, error=error)
        return
    logger.info(f"generate_dataset: assembled {len(dataset_info)} dataset item(s)")
    yield Step(message=f"已组装 {len(dataset_info)} 条问题-SQL 对", status=StepStatus.SUCCESS)

    # 1. 以 SQL_BATCH_SIZE 条为单位用 EXPLAIN 校验 SQL，收集报错的 SQL（key 为条目在数据集中的序号）
    yield Step(message=f"正在校验 SQL（每 {SQL_BATCH_SIZE} 条一批）")
    items = []
    for start in range(0, len(dataset_info), SQL_BATCH_SIZE):
        batch = dataset_info[start:start + SQL_BATCH_SIZE]
        results = await gather(*(explain_sql(x.sql, database_id) for x in batch))
        for offset, (valid, error) in enumerate(results):
            if valid:
                continue
            items.append({
                "key": start + offset + 1,
                "question": batch[offset].question,
                "sql": batch[offset].sql,
                "error": error or "SQL is invalid",
            })
    logger.info(f"generate_dataset: {len(items)} of {len(dataset_info)} SQL need correction")

    # 2. 报错的 SQL（连同问题、错误与可用表）一次性交给模型纠正，按 key 写回，其余条目维持原 SQL
    if items:
        yield Step(message=f"收集到 {len(items)} 条报错的 SQL，正在纠正")
        payload = {"items": items, "tables": tables}
        records, error = await invoke(model, [
            SystemMessage(content=EVALUATION_SQL_CORRECTION_PROMPT),
            HumanMessage(content=dump(payload, default=str)),
        ], SqlList)
        if error:
            logger.error(error)
            yield Step(message=f"SQL 纠正失败：{error}", status=StepStatus.FAILED, error=error)
            return
        corrected = {record.key: record.sql for record in records}
        logger.info(f"generate_dataset: corrected {len(corrected)} SQL")
        yield Step(message=f"SQL 纠正完成：共纠正 {len(corrected)} 条", status=StepStatus.SUCCESS)
        dataset_info = [
            DatasetItemInfo(question=x.question, sql=corrected.get(index, x.sql))
            for index, x in enumerate(dataset_info, start=1)
        ]
    else:
        yield Step(message=f"SQL 校验通过：{len(dataset_info)} 条均无需纠正", status=StepStatus.SUCCESS)

    # 3. 落库：一条 Dataset（库上下文、会话编号）加逐条 DatasetItem。
    # reference_contexts 存下出题所用的表结构：判定两条 SQL 是否等效要看字段的业务含义，存下来既让数据集
    # 可复现，也让后续评测不必依赖当时的元数据状态（见 models.dataset.Dataset.reference_contexts）。
    # 数据集与条目在一个事务里写入，避免留下一条没有条目的数据集；名称按生成时间取，同一天存多份互不覆盖。
    yield Step(message="正在保存数据集")
    async with in_transaction(META_CONNECTION):
        dataset = await Dataset.create(name=f"数据集 {datetime.now():%Y-%m-%d %H:%M:%S}",
                                       thread_id=thread_id,
                                       reference_contexts=[asdict(x) for x in table_infos])
        await DatasetItem.bulk_create([
            DatasetItem(question=x.question, reference=x.sql, dataset_id=dataset.id)
            for x in dataset_info
        ])
    logger.info(f"generate_dataset: saved dataset {dataset.id} with {len(dataset_info)} item(s)")
    yield Step(message=f"数据集已保存：共 {len(dataset_info)} 条", status=StepStatus.SUCCESS,
               dataset_id=dataset.id)


async def evaluate_item(item: DatasetItemInfo,
                        agent: RetrievalAgent,
                        metric: SQLSemanticEquivalence,
                        database_id: int,
                        schema: str,
                        semaphore: Semaphore) -> tuple[str | None, float, str | None, list[dict]]:
    """单条评测：先跑检索得到待评 SQL，再与参考 SQL 比对打分。

    :param item: 待评测的条目（问题与参考 SQL）
    :param agent: 待评测的智能体：按问题跑一轮（指标 → 表 → SQL），产出待评 SQL
    :param metric: SQL 语义等效性指标（见 build_sql_equivalence）
    :param database_id: 待评测的业务库编号：检索智能体在该库上答题
    :param schema: 出题与判定所用的表结构文本，作为判定的数据库上下文交给指标
    :param semaphore: 并发额度，检索与指标调用共用：两者都是大模型请求，同时放开会触发服务端限流
    :return: (检索生成的 SQL, 等效性得分, 得分理由, 检索到的表信息)：SQL 未生成或没通过校验时为 None，得分为
        1.0（等效）/ 0.0（不等效或未生成 SQL），理由为指标的判定说明或失败原因；表信息是随着这条 SQL 一起
        检索到的表、字段与取值（逐条转成字典，供落库成 Result.retrieved_contexts），没有可用的 SQL 时为空列表

    检索没交出 SQL（没生成，或生成了但没通过校验）时不调用指标，直接记 0 分；单条失败（检索或指标抛异常）
    不向上抛，原因写进返回的理由，故一条失败不影响其余条目。
    """
    score, reason, response, tables_infos = 0.0, None, None, []
    try:
        async with semaphore:
            response, table_infos = await agent.eval(item.question, database_id)
            # 新智能体的表信息本身就是字典（TableInfo 是 TypedDict），直接拷一份即可
            tables_infos = [dict(x) for x in table_infos]
            if not response:
                reason = "检索未生成 SQL"
            else:
                result = await metric.ascore(
                    response=response,
                    reference=item.sql,
                    reference_contexts=[schema]
                )
                score, reason = result.value, result.reason
    except Exception as e:  # noqa: BLE001 - 单条失败只记原因，不拖垮整轮评测
        logger.error(e)
        reason = str(e)
    return response, score, reason, tables_infos


async def evaluate_dataset(dataset_id: int, database_id: int) -> AsyncGenerator[Step, None]:
    """评测一份已落库的数据集：逐条跑检索、按 SQL 语义等效性打分，结果与解读报告落库。

    :param dataset_id: 数据集编号（Dataset.id）：问题与参考 SQL 从它的条目里取
    :param database_id: 待评测的业务库编号（Database.id）：检索智能体在该库上回答问题
    :return: 逐条进度帧（都带 dataset_id），失败帧带原因

    1. 从 Dataset.reference_contexts 还原出题所用的表编号，按编号重新取一遍表信息（表描述、字段描述 /
       别名 / 角色、字段取值，见 build_table_infos）：出题到评测之间元数据可能被改过或重新采集过，
       而判定与解读报告都要靠这些信息，用最新的一份才判得准；
    2. 每条问题各起一个任务并发跑智能体（agent.eval 带上业务库编号），得到待评 SQL 与检索到的表信息，
       并发度由 EVALUATION_CONCURRENCY 限制；
    3. 用指标（Ragas 的 SQL 语义等效性）把待评 SQL 与参考 SQL 比对，得 1.0（等效）/ 0.0（不等效）；
       检索没产出 SQL 时不调用指标，直接记 0 分；单条失败不影响其余条目，原因写进该条的理由；
    4. 结果按问题原顺序归位后，把表元信息与逐条结果交给模型，按 EVALUATION_REVIEW_PROMPT 写一份解读报告
       ——整体结论、逐条归因、共性问题与改进建议（纯文本）；报告生成失败只记日志与一条失败帧，
       不影响这一轮评测结果本身；
    5. 结果、解读报告与检索到的表信息一起落库：一条 Result（mean / report / retrieved_contexts）
       与逐条 ResultItem，明细不进内存状态、按编号从库里取。
    """
    dataset = await Dataset.get_or_none(id=dataset_id)
    if dataset is None:
        error = f"数据集 {dataset_id} 不存在，无法评测"
        logger.warning(f"evaluate_dataset: {error}")
        yield Step(message=error, status=StepStatus.FAILED, error=error)
        return

    items = await DatasetItem.filter(dataset_id=dataset_id).order_by("id")
    dataset_info = [DatasetItemInfo(question=item.question, sql=item.reference) for item in items]
    if not dataset_info:
        error = "数据集为空，无法评测"
        logger.warning(f"evaluate_dataset: {error}")
        yield Step(message=error, status=StepStatus.FAILED, error=error, dataset_id=dataset_id)
        return

    # 出题所用的表编号存的是 EvalTableInfo 快照，表编号就在每张表的 id 上；表后来被删掉的自然从上下文里去掉
    table_ids = [x["id"] for x in dataset.reference_contexts or [] if isinstance(x, dict) and x.get("id")]
    if not table_ids:
        logger.warning(f"evaluate_dataset: dataset {dataset_id} has no reference contexts, "
                       f"judge without schema")
    table_infos = await build_table_infos(table_ids, database_id)
    # 表结构作为判定的数据库上下文：判断两条 SQL 是否等价要看字段语义，模型只有知道字段的业务含义才判得准
    schema = render_schema(table_infos)

    yield Step(message=f"正在评测 {len(dataset_info)} 条问题（并发 {EVALUATION_CONCURRENCY}）",
               dataset_id=dataset_id)
    # 评估是批量的：不带 checkpointer 的图在第一次 eval 时就地编译、整批复用（见 RetrievalAgent.eval）
    agent = RetrievalAgent()
    # 对话模型按 model 表里的默认登记取，判等指标与解读报告共用同一个实例
    model = await fetch_chat_model()
    metric = build_sql_equivalence(model)
    semaphore = Semaphore(EVALUATION_CONCURRENCY)

    # 字典以任务为键、条目序号为值：先到的先上报进度，完成后按序号归位，避免完成顺序打乱数据集顺序。
    # 必须用 async for 迭代：as_completed 在普通 for 下产出的是它内部的协程（拿不到任务本身），
    # 是 Python 3.14 起的新语义
    tasks = {create_task(evaluate_item(item, agent, metric, database_id, schema, semaphore)): index
             for index, item in enumerate(dataset_info)}
    results = []
    async for task in as_completed(tasks):
        results.append((tasks[task], await task))
        yield Step(message=f"评测进度：{len(results)}/{len(tasks)}", dataset_id=dataset_id)

    # 结果按问题原顺序归位：(检索生成的 SQL, 得分, 理由, 检索到的表信息)，顺序与 dataset_info 一致
    evaluations = [result for _, result in sorted(results)]
    mean = sum(score for _, score, _, _ in evaluations) / len(evaluations)
    logger.info(f"evaluate_dataset: {sum(score for _, score, _, _ in evaluations):.0f}/{len(evaluations)} "
                f"item(s) equivalent, mean={mean:.4f}")
    yield Step(message=f"评测完成：SQL 等效率 {mean:.4f}", status=StepStatus.SUCCESS, dataset_id=dataset_id)

    # 解读报告：把表元信息与本轮逐条结果交给模型，写一段「整体结论 + 逐条归因 + 共性问题 + 改进建议」，
    # 随结果一起落库（Result.report）。报告只是解读，生成失败不影响这一轮评测结果本身。
    yield Step(message="正在生成评测解读报告", dataset_id=dataset_id)
    payload = {
        "mean": mean,
        "items": [
            {
                "question": item.question,
                "reference_sql": item.sql,
                "response_sql": answer,
                "score": score,
                "reason": reason,
            }
            for item, (answer, score, reason, _) in zip(dataset_info, evaluations)
        ],
        "tables": [asdict(x) for x in table_infos],
    }
    report, error = await invoke(model, [
        SystemMessage(content=EVALUATION_REVIEW_PROMPT),
        HumanMessage(content=dump(payload, default=str)),
    ], EvaluationReview)
    if error:
        logger.error(error)
        yield Step(message=f"解读报告生成失败：{error}", status=StepStatus.FAILED, error=error,
                   dataset_id=dataset_id)

    # 检索到的表信息：逐条问题检索出来的表按问题顺序依次排开，随结果一起落库、不去重（同一张表被多条问题
    # 检索到就存多份，各条问题检索到什么就是什么），作为这一轮的检索片段快照
    # （见 models.dataset.Result.retrieved_contexts）。表信息在 evaluate_item 里已逐条转成字典。
    retrieved_contexts = [table_info for _, _, _, table_infos in evaluations for table_info in table_infos]

    # 落库：一条 Result（平均分、解读报告与检索到的片段）加逐条 ResultItem（参考答案、待评 SQL 与判定理由）。
    # 参考答案直接从本轮的数据集条目抄下来：dataset_info 与 evaluations 同序、一一对应，存下参考 SQL 就不必
    # 再回表取条目。结果与条目在一个事务里写入，避免留下没有条目的评测结果。
    async with in_transaction(META_CONNECTION):
        result = await Result.create(dataset_id=dataset_id, mean=mean, report=report,
                                     retrieved_contexts=retrieved_contexts)
        await ResultItem.bulk_create([
            ResultItem(result_id=result.id, reference=item.sql, answer=answer, score=score, reason=reason)
            for item, (answer, score, reason, _) in zip(dataset_info, evaluations)
        ])
    logger.info(f"evaluate_dataset: saved result {result.id} of dataset {dataset_id}")
    yield Step(message=f"评测结果已保存：SQL 等效率 {mean:.4f}", status=StepStatus.SUCCESS,
               dataset_id=dataset_id)
