"""第三章园区运营总览指标专属 Prompt（独立文件）"""
import time
def get_prompt(company=None, start_date=None, end_date=None, dimension=None, 
               dimension_data=None, max_words=None, **kwargs):
    """
    返回第三章的完整 Prompt 模板
    模板中预留变量：{company}, {start_date}, {end_date}, {dimension}, {dimension_data}, {max_words}
    """
     # 处理传入的参数，如果为None则使用默认值
    company = company or "未知公司"
    start_date = start_date or "未知时间"
    end_date = end_date or "未知时间"
    dimension = dimension or "综合"
    dimension_data = dimension_data or "暂无数据"
    max_words = max_words or 500
    prompt_template = """
你是专业的智慧园区运营报告生成专家，请严格按照以下要求生成内容：

### 基础信息
- 公司名称：{company}
- 统计周期：{start_date} 至 {end_date}
- 分析维度：{dimension}

### 数据支撑
{dimension_data}

### 生成规则
1.  字数控制：表格整体内容（含标题）严格控制在 {max_words} 字以内，避免冗余。
2.  核心要求：整合所有维度的分析概要，形成单一 Markdown 表格，不得拆分多个表格，表格需紧跟二级标题下方，行高随内容长度自适应；
3.  表头固定（按以下顺序排列，不得增减、不得调整顺序）：序号、KPI、计算公式、本期值、环比（%）、同比（%）、目标值、备注，行高随内容长度自适应；
4.  数据排列规则（严格遵守，不得调整）：
    1.  第一行必须为“综合健康指数”，不得调整顺序、不得替换名称；
    2.  后续行按“安防、能耗、运营”的优先级排列（无对应维度则跳过，不出现多余行），每个维度对应1行核心KPI数据；
    3.  计算公式需准确对应各KPI的统计逻辑（从dimension_data中提取，不得编造、不得简化）；
    4.  本期值需提取实际数据并附带对应单位，核心数据（数值部分）必须加粗，格式规范,内容中不得出现**；
    5.  备注需简洁明了，说明数据来源、异常情况或特殊说明（如“低于阈值”“参考行业标准”“夜间占比31%”），无特殊情况可填写“数据来源：园区运营系统”；
5.  语言风格：正式、客观、严谨，表格内容规范，无口语化表达、无虚构数据；

### 呈现形式（强制要求）
1.  表格格式标准：表头对齐，行列清晰，序号连续（1、2、3...），无缺失行、无重复行；
2.  参考格式（仅为结构示例，需替换为dimension_data中的实际数据，不得照搬示例数据）：
| 序号 | KPI | 计算公式 | 本期值 | 环比（%） | 同比（%） | 目标值 | 备注 |
|---|---|---|---|---|---|---|---|
| 1 | 综合健康指数 | Σ(维度×权重)/Σ权重 | **88.6** | +3.2 | +5.8 | ≥85 | 由模型自动加权 |
| 2 | 安防告警总数 | 计数(所有告警事件) | **124** | -18 | -12 | ≤150 | 低于阈值 |
| 3 | 能耗总量（MWh） | Σ(电力+水+燃气消耗) | **1842** | +5.1 | -4.3 | — | 同比10%为目标 |
| 4 | 门禁通行人次 | 计数(刷卡+人脸识别) | **1587230** | +2.3 | +1.8 | — | 夜间占比31% |

### 输出格式要求
1.  确保表格无格式错误（无缺失竖线、无错乱对齐、无重复表头）。
2.  直接输出表格内容，无需额外标题、无需解释说明、无需多余引言；
3.  表格数据必须真实提取自dimension_data，计算公式、单位、备注准确对应，不得编造；

"""
    formatted_prompt = prompt_template.format(
        company=company,
        start_date=start_date,
        end_date=end_date,
        dimension=dimension,
        dimension_data=dimension_data,
        max_words=max_words,
       
    )
    
    return formatted_prompt
# 该章节专属配置（适配表格+标题格式，预留Token冗余）
CHAPTER_CONFIG = {
    "max_words": 800,  # 适配表格多行数据，合理控制长度
    "max_token_length": 1200,  # 增加Token冗余，容纳标题、表格符号和数据格式
    "data_truncation_length": 1000,  # 保留足够数据用于提取KPI和计算公式
    "title_format_check": True  # 标记需校验标题格式和表格规范
}