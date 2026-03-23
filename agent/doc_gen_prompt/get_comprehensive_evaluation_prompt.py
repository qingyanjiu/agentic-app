"""第五章综合评估专属 Prompt（独立文件）"""
import time
def get_prompt(company=None, start_date=None, end_date=None, dimension=None, 
               dimension_data=None, max_words=None, **kwargs):
    """
    返回第五章综合评估的完整 Prompt 模板
    模板中预留变量：{company}, {start_date}, {end_date}, {dimension}, {dimension_data}, {max_words}
    """
        # 处理传入的参数，如果为None则使用默认值
    company = company or "未知公司"
    start_date = start_date or "未知时间"
    end_date = end_date or "未知时间"
    dimension = dimension or "综合"
    dimension_data = dimension_data or "暂无数据"
    # 获取当前时间
    current_time = time.strftime("%Y-%m-%d %H:%M")
    prompt_template = """
你是专业的智慧园区运营报告生成专家，请严格按照以下要求生成，确保内容完整、逻辑连贯、数据精准。

### 基础信息
- 公司名称：{company}
- 统计周期：{start_date} 至 {end_date}
- 分析维度：{dimension}
- 核心字数限制：{max_words} 字左右（误差不得超过±50字，超出或不足将视为不合格）

### 数据支撑
{dimension_data}


### 生成规则（严格遵守）
1.  标题层级强制规范：
    - 核心子模块：必须使用 ### 开头（三级标题），对应以下4个固定部分，按顺序排列，不得增减、不得调整顺序；
    - 四级标题（####）：禁止使用（无需细分模块，避免冗余）；
    - 禁止使用#（一级）、#####及以上层级标题，杜绝格式混乱。

2.  核心内容要求（必须包含以下4部分，逻辑连贯，层层递进，不得遗漏任一模块）：
    ### 5.1 综合健康指数趋势
    基于过去{start_date} 至 {end_date}的综合健康指数数据，分析指数变化规律，明确说明季节性波动特征（需结合园区实际运营场景，示例：“夏季因高温导致能耗上升、安防告警量增加，指数略有下滑；节假日园区人流减少，指数呈小幅上升趋势”），无需冗余铺垫，直接聚焦趋势和波动原因。

    ### 5.2 关键风险点
    从各维度{dimension}分析中提炼最核心的3个风险点，每个风险点需详细说明**具体表现**和**财务影响**（量化数据支撑，示例：“1. 安防告警量偏高：月度平均告警量150次，较标准值超出20%，导致安防维护费用每年增加15万元，增加运营成本压力”），3个风险点分点阐述，逻辑清晰，禁止泛泛而谈。

    ### 5.3 预算/费用影响
    明确列出本期核心运营费用，需包含4类必选费用（能源费用、安防维护费用、网络运维费用、总运营成本），每类费用需标注具体金额及单位，同时说明费用变化趋势（同比/环比变化、变化原因，示例：“能源费用本期180万元，同比上升5%，主要因夏季高温导致空调能耗增加”），数据需提取自dimension_data，不得编造。

    ### 5.4 趋势预测与决策点
    基于回归模型或LLM+时间序列模型，预测下季度核心指标（至少包含综合健康指数、能耗、网络利用率3项），预测结果需合理、有数据支撑；同时明确下季度需提交的资本支出（CAPEX）和运营预算（OPEX），标注具体金额及用途，贴合园区运营实际需求。

3.  语言风格：正式、严谨、客观，符合企业运营报告调性，禁止口语化表达、主观臆断，所有结论需有数据支撑（提取自{dimension_data}）。
4.  结构要求：4个三级子模块按顺序排列，模块间有自然过渡，整体形成完整的综合评估逻辑，不得拆分模块、颠倒顺序。

### 输出格式要求
1.  严格遵守标题层级规范，下方依次排列4个三级子模块（### 5.1-5.4），再往下用有序标签展示；
2.  直接输出章节内容，无需额外标题、无需解释说明、无需多余引言，模块内内容连贯，无冗余；
3.  控制总字数在{max_words}±50字范围内，重点突出核心结论和量化数据；
4.  禁止出现任何违规标题符号，禁止拆分模块、遗漏核心内容，确保数据精准、逻辑连贯。
"""
    formatted_prompt = prompt_template.format(
        company=company,
        start_date=start_date,
        end_date=end_date,
        dimension=dimension,
        dimension_data=dimension_data,
        max_words=max_words,
        current_time=current_time  # 补充时间变量（模板中提到但未替换的）
    )
    
    return formatted_prompt
# 第五章专属配置（适配字数和内容复杂度，预留Token冗余）
CHAPTER_CONFIG = {
    "max_words": 550,  # 核心字数要求，误差±50字
    "max_token_length": 800,  # 增加Token冗余，容纳标题、格式和量化数据
    "data_truncation_length": 1200,  # 保留足够数据用于提炼风险、费用、预测指标
    "title_format_check": True,  # 标记需校验标题格式和内容完整性
    "word_count_check": True  # 新增：标记需校验字数是否符合要求
}