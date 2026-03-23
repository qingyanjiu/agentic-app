"""报告概述章节专属 Prompt（独立文件）"""
import time
def get_prompt(company=None, start_date=None, end_date=None, dimension=None, 
               dimension_data=None, max_words=None, **kwargs):
    """
    返回报告概述的完整 Prompt 模板
    模板中预留变量：{company}, {start_date}, {end_date}, {dimension}, {dimension_data}, {max_words}
    """
     # 处理传入的参数，如果为None则使用默认值
    company = company or "园区"
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
1. 标题层级强制规范：
   - 核心子模块标题：必须使用 ### 开头（三级标题），对应以下4个固定模块；
   - 细分项需四级标题（####），仅在有特殊细分维度时使用（非必需）；
2. 字数严格控制在 {max_words} 字以内，突出园区整体运营定位；
3. 需结合 {start_date}-{end_date} 的时间特征（如季度末、年度复盘）；
4. 语言风格：正式、简洁、客观，符合报告开篇的专业性；
5. 禁止出现口语化表达，禁止虚构数据；

### 呈现形式（强制要求）
1. 核心信息必须采用 Markdown 表格呈现，行高随内容长度自适应；
2. 表格表头固定为「序号」「维度」「发现（简要）」「推荐措施（SMART原则）」「预计收益/风险降低」「优先级」「时间节点」「预算（万元）」（按此顺序排列）；
3. 表格行规则：
   | 序号 | 维度 | 发现（简要） | 推荐措施（SMART原则） | 预计收益/风险降低 | 优先级 | 时间节点 | 预算（万元） |
   |------|------|--------------|-----------------------|-------------------|--------|----------|--------------|
   | 1    | 示例维度 | 示例发现（含量化数据） | 示例措施（符合SMART） | 示例量化效果 | 高/中/低 | YYYY-MM-DD | 示例数值 |
   | 2    | ...  | ...          | ...                   | ...               | ...    | ...      | ...          |
4. 序号要求：按1、2、3...连续编号，每项单独为一行；
5. 维度要求：明确对应所属维度（如“安防”“能耗”）；
6. 发现（简要）要求：简洁描述问题核心，必须包含量化数据；
7. 推荐措施要求：严格遵循SMART原则（具体、可衡量、可实现、相关性、时限性），明确措施内容、完成时间；
8. 预计收益/风险降低要求：量化说明措施效果（如“入侵事件预防率提升15%”“年耗电降低5%”）；
9. 优先级要求：仅允许标注“高/中/低”；
10. 时间节点要求：格式必须为“YYYY-MM-DD”；
11. 预算（万元）要求：填写具体数值（如0、5.8、10）。

### 输出格式要求
1. 严格遵守标题层级：仅允许使用###（三级）标题，禁止使用#、##、####及以上层级标题；
2. 直接输出章节内容，无需额外标题和解释说明；
3. 表格格式必须标准，字段顺序与内容严格匹配，生成时间需按指定格式填充当前系统时间。
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
# 可选：定义该章节的专属配置（适配标题/表格格式的Token冗余）
CHAPTER_CONFIG = {
    "max_words": 300,
    "max_token_length": 600,  # 增加100 Token冗余，容纳标题和表格符号
    "data_truncation_length": 800,  # 该章节数据最大字符数
    "title_format_check": True  # 新增：标记需校验标题格式
}