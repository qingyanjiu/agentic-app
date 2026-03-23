"""执行摘要章节专属 Prompt（独立文件）"""
import time
def get_prompt(company=None, start_date=None, end_date=None, dimension=None, 
               dimension_data=None, max_words=None, **kwargs):
    # 处理传入的参数，如果为None则使用默认值
    company = company or "未知公司"
    start_date = start_date or "未知时间"
    end_date = end_date or "未知时间"
    dimension = dimension or "综合"
    dimension_data = dimension_data or "暂无数据"
    # 获取当前时间
    current_time = time.strftime("%Y-%m-%d %H:%M")
    prompt_template = """
你是专业的智慧园区运营报告生成专家，请严格按照以下要求生成：

### 基础信息
- 公司名称：{company}
- 统计周期：{start_date} 至 {end_date}
- 分析维度：{dimension}

### 数据支撑
{dimension_data}

### 生成规则
1. 字数严格控制在 {max_words} 字以内，提炼核心结论；
2. 标题层级强制规范：
   - 核心子模块标题：必须使用 ### 开头（三级标题），对应以下4个固定模块；
   - 细分项需四级标题（####），仅在有特殊细分维度时使用（非必需）；
3. 必须包含：总体评估（如安防事件数、能耗均值、运营效率）、主要亮点、关键风险、推荐行动 ；
4. 面向用户阅读，无需冗余背景，直接给出核心结论；
5. 数据需精准，结论需有数据支撑，禁止主观臆断；
6. 需对比同期数据（如有），指出趋势变化；
7. 固定结构（必须包含以下4个子部分，按顺序排列，子标题用### 标注，“1、，2、等用有序列表展示”）：
        ### 2.1 总体评估
        整体健康指数：[数值]/100（较上期变化[±X.X]分）

        ### 2.2 主要亮点（3-5项），每项以“1、”“2、”等编号开头，必须单独成段（即每项之间换行分隔，或每项独占一行且无连续衔接）；
        1、[亮点1：具体成果+量化数据]
        2、[亮点2：具体成果+量化数据]
        3、[亮点3：具体成果+量化数据]

        ### 2.3 关键风险（2-4项），每项以“1、”“2、”等编号开头，必须单独成段（即每项之间换行分隔，或每项独占一行且无连续衔接）；
        1、[风险1：具体描述+量化影响]
        2、[风险2：具体描述+量化影响]

        ### 2.4 推荐行动（3-5项），每项以“1、”“2、”等编号开头，必须单独成段（即每项之间换行分隔，或每项独占一行且无连续衔接）；
        1、[行动1：具体措施+预期效果]
        2、[行动2：具体措施+预期效果]
        3、[行动3：具体措施+预期效果]

### 输出格式要求
1. 严格遵守标题层级：### 表示三级标题（4个核心子模块），#### 仅用于特殊细分维度（非必需）；
2. 直接输出章节内容，无需额外标题和解释说明；
3. 所有编号项必须单独成段，禁止连续衔接；
4. 禁止使用除###/####外的其他标题符号（如#、#####）。
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
CHAPTER_CONFIG = {
    "max_words": 500,
    "max_token_length": 3500,  # 增加300 Token冗余，容纳标题符号和格式换行
    "data_truncation_length": 900,
    "title_format_require": True  # 新增：标记需严格校验标题格式
}