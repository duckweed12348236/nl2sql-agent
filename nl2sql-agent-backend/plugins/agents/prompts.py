"""采集与评估用的提示词：按用途分节，正文放在同目录的 prompts/*.md 里。

检索链路的提示词已经不属于这里——判断题在 skills/ 下的技能里，调度在外层的系统提示词里（见 agnets）。
加载即读盘，模块导入时一次性读入，热改文案需重启进程。
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8")


# ── 重排指令（rerank 的 instruct 参数）：检索侧还在用，说明文档是什么、按什么标准判断相关性 ─────────

TABLE_RERANK_INSTRUCT = _load("table_rerank.md")

METRIC_RERANK_INSTRUCT = _load("metric_rerank.md")

# 采集用的表结构分析提示词不在这里：它随输入形态（自然语言文本）一起放在 agnets.collection
# 里（见那里的 TABLE_DISCOVER_PROMPT），本模块只管重排与评估这几份。

# ── 评估：出题、批量生成 / 纠正参考 SQL、评测解读 ────────────────────────────────────────────

QUESTION_GENERATION_PROMPT = _load("evaluation_question_generation.md")

EVALUATION_SQL_GENERATION_PROMPT = _load("evaluation_sql_generation.md")

EVALUATION_SQL_CORRECTION_PROMPT = _load("evaluation_sql_correction.md")

EVALUATION_REVIEW_PROMPT = _load("evaluation_review.md")
