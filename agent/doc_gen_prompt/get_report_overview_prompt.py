"""报告概述章节专属 Prompt（独立文件）"""
import time
from datetime import datetime, timedelta
def get_prompt(company=None, start_date=None, end_date=None, dimension=None, 
               dimension_data=None, max_words=None, **kwargs):
    """
    返回报告概述的完整 Prompt 模板
    模板中预留变量：{company}, {start_date}, {end_date}, {dimension}, {dimension_data}, {max_words}
    """
    # 处理传入的参数，如果为None则使用默认值
    # company = company or "园区"
    # start_date = start_date or "未知时间"
    # end_date = end_date or "未知时间"
    # dimension = dimension or "综合"
    # dimension_data = dimension_data or "暂无数据"
    # max_words = max_words or 500
    
    # 获取北京时间（正确的当前时间）
    utc_now = datetime.utcnow()
    beijing_now = utc_now + timedelta(hours=8)
    current_time = beijing_now.strftime("%Y-%m-%d %H:%M")

    prompt_template = """
你是专业的智慧园区运营报告生成专家，请严格按照以下要求生成：

### 基础信息
- 公司名称：{company}
- 统计周期：{start_date} 至 {end_date}
- 分析维度：{dimension}

### 数据支撑
{dimension_data}

### 生成规则
1. 需结合 {start_date}-{end_date} 的时间特征（如季度末、年度复盘）。
2. 字数严格控制在 {max_words} 字以内，突出园区整体运营定位；
3. 内容需包含：报告周期、报告版本、编制部门、生成时间、分析维度；
4. 语言风格：正式、简洁、客观，符合报告开篇的专业性；
5. 禁止出现口语化表达，禁止虚构数据；

### 呈现形式（强制要求）
1. 核心信息必须采用两列五行的 Markdown 表格呈现；
2. 表格表头固定为「名称」「内容」，行高随内容长度自适应；
3. 表格必填字段（按以下顺序排列，不得增减、不得修改字段名）：
   | 名称       | 内容                                   |
   |------------|---------------------------------------|
   | 报告周期   | {start_date}~{end_date}                |
   | 报告版本   | V1.0                                   |
   | 编制部门   | 园区数字化运营中心                       |
   | 生成时间   | {current_time}                         |
   | 分析维度   | {dimension}                            |

### 输出格式要求
1. 表格格式必须标准，字段顺序与内容严格匹配，生成时间需按指定格式填充当前系统时间。
2. 直接输出章节内容，无需额外标题和解释说明；
"""
    formatted_prompt = prompt_template.format(
        company=company or "园区",
        start_date=start_date or "未知时间",
        end_date=end_date or "未知时间",
        dimension=dimension or "综合",
        dimension_data=dimension_data or "未知数据",
        max_words=max_words,
        current_time= current_time  # 补充时间变量（模板中提到但未替换的）
    )
    
    return formatted_prompt
# 可选：定义该章节的专属配置（适配标题/表格格式的Token冗余）
CHAPTER_CONFIG = {
    "max_words": 300,
    "max_token_length": 600,  # 增加100 Token冗余，容纳标题和表格符号
    "data_truncation_length": 800,  # 该章节数据最大字符数
    "title_format_check": True  # 新增：标记需校验标题格式
}