"""安防维度分析 Prompt（真实数据口径）。"""

from ._common import build_dimension_prompt

_FOCUS = """1. 告警总量与今日告警量；
2. 告警类型分布（各类型数量与占比）；
3. 告警时间趋势（有日期序列时说明峰值与走势）；
4. 安全指数、AI 识别准确率、算法类型数量；
5. 巡查任务数量与执行情况（有数据时）。
重点关注告警高发区域与异常时段，但不要编造区域分布。"""


def get_prompt(company=None, start_date=None, end_date=None,
               dimension_data=None, max_words=None, **kwargs):
    return build_dimension_prompt(
        "安防",
        _FOCUS,
        company=company,
        start_date=start_date,
        end_date=end_date,
        dimension_data=dimension_data,
        max_words=max_words,
    )


# 该章节专属配置（适配表格+标题格式，预留Token冗余）
CHAPTER_CONFIG = {
    "max_words": 500,
    "max_token_length": 1200,
    "data_truncation_length": 2400,
    "title_format_check": True,
}
