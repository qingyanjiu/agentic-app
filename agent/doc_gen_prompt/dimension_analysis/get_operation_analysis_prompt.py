"""运营维度分析 Prompt（真实数据口径）。"""

from ._common import build_dimension_prompt

_FOCUS = """1. 园区面积与 IoT 设备总数；
2. 人员态势：实时在园人数、今日进入/离开人数；
3. 车辆态势：车位总数、已用、剩余；
4. 食堂就餐人次；
5. 会议场次与平均会议时长；
6. 巡检任务数、巡检点位、完成率；
7. 设备健康分、设备在线率、设备分类数量。
重点关注各子系统的运行效率与异常点，但不要编造未采集到的指标。"""


def get_prompt(company=None, start_date=None, end_date=None,
               dimension_data=None, max_words=None, **kwargs):
    return build_dimension_prompt(
        "运营",
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
