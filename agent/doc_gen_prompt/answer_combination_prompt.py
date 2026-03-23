"""报告概述章节专属 Prompt（独立文件）"""
import time
def get_prompt(company=None, start_date=None, end_date=None, dimension=None, 
               dimension_data=None, max_words=None, **kwargs):
    """
    返回报告概述的完整 Prompt 模板
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
    return """
请为【{company}】公司{start_date}-{end_date}智慧园区运营报告撰写「答案组合」章节。

## 已生成章节摘要
{chapters_content}

## 撰写要求（{max_words}字以内）
1. 整合各章节核心发现：安防、能耗、运营维度的关键结论
2. 提炼3-5个最重要的洞察（必须有数据支撑）
3. 总结2-3个主要问题和改进方向
4. 给出未来运营优化的建议

## 输出格式
### 核心发现
• [发现1]：结论 + 数据
• [发现2]：结论 + 数据

### 问题与改进
• [问题] → [改进建议]

### 未来展望
[1段展望]

请开始撰写：
"""

# 可选：定义该章节的专属配置（适配标题/表格格式的Token冗余）
CHAPTER_CONFIG = {
    "max_words": 300,
    "max_token_length": 600,  # 增加100 Token冗余，容纳标题和表格符号
    "data_truncation_length": 800,  # 该章节数据最大字符数
    "title_format_check": True  # 新增：标记需校验标题格式
}