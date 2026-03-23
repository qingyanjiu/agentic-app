"""报告概述章节专属 Prompt（独立文件）"""
import time
def get_prompt(query=None,company=None, start_date=None, end_date=None, dimension=None, 
               agent_out=None, max_words=None, **kwargs):
    """
    返回报告概述的完整 Prompt 模板
    模板中预留变量：{company}, {start_date}, {end_date}, {dimension}, {dimension_data}, {max_words}
    """
    # 处理传入的参数，如果为None则使用默认值
    query=query or "园区综合运营报告"
    company = company or "园区"
    dimension = dimension or "综合"
    agent_out = agent_out or "暂无数据"

    prompt_template= """
你是一个专业的报告评估专家。请对以下生成的智慧园区运营报告的各章节进行质量评估：
用户问题: {state['query']}
报告内容：: {agent_out}
公司名称：{company}
分析维度：{dimension}

        请对每个章节从以下维度进行评分（1-10分）：
        1. 内容完整性：是否覆盖了该章节应包含的核心内容
        2. 数据准确性：是否基于数据进行分析，数据引用是否准确
        3. 逻辑清晰度：论述是否条理清晰，逻辑严密
        4. 语言专业性：语言是否专业、正式

        评分标准：
        - 9-10分：优秀，无需修改
        - 7-8分：良好，但有小瑕疵
        - 5-6分：及格，有明显不足
        - 1-4分：不及格，需要重写

        请以JSON格式返回评估结果，格式如下：
        {{
            "报告概述": {{"score": 8, "issues": ["问题1", "问题2"], "needs_retry": false}},
            "执行摘要": {{"score": 5, "issues": ["内容不够简洁", "关键指标缺失"], "needs_retry": true}},
            ...
        }}

        注意：只有评分低于3分的章节才设置"needs_retry": true
"""
    formatted_prompt = prompt_template.format(
        query=query,
        company=company,
        agent_out=agent_out,
        dimension=dimension,
    )
    return formatted_prompt
# 可选：定义该章节的专属配置（适配标题/表格格式的Token冗余）
CHAPTER_CONFIG = {
    "max_words": 3000,
    "max_token_length": 6000,  # 增加100 Token冗余，容纳标题和表格符号
    "data_truncation_length": 8000,  # 该章节数据最大字符数
    "title_format_check": True  # 新增：标记需校验标题格式
}