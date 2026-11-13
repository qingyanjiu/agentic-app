"""能耗维度分析 Prompt（真实数据口径）。"""

from ._common import build_dimension_prompt

_FOCUS = """1. 累计用电量与今日用电量（千瓦时）；
2. 累计用水量与今日用水量（吨）；
3. 电表 / 水表数量；
4. 能耗设备在线与离线台数；
5. 用电、用水区域排名（有排名数据时列出前几名）。
重点关注高耗能区域与设备在线情况，但不要编造排名或同比数据。"""


def get_prompt(company=None, start_date=None, end_date=None,
               dimension_data=None, max_words=None, **kwargs):
    return build_dimension_prompt(
        "能耗",
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
