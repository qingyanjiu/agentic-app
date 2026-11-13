"""维度深度分析 Prompt 公共构造器（真实业务数据版）。

三个维度（安防 / 能耗 / 运营）共用同一套报告骨架，差异只在「分析重点」。
数据部分直接注入 DocDataCollector 采集到的 summary + overview，
不再假设 MCP 返回里一定有某个固定字段，避免真实接口下出现满屏「数据缺失」。
"""

import json


def _load_data(dimension_data):
    """dimension_data 可能是 dict，也可能是被截断的 JSON 字符串。"""
    if isinstance(dimension_data, dict):
        return dimension_data
    if isinstance(dimension_data, str):
        try:
            parsed = json.loads(dimension_data)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
        return {"type": "未知", "summary": dimension_data, "overview": {}}
    return {"type": "未知", "summary": "", "overview": {}}


def build_dimension_prompt(
    dimension: str,
    focus: str,
    company=None,
    start_date=None,
    end_date=None,
    dimension_data=None,
    max_words=None,
    **kwargs,
) -> str:
    company = company or "园区"
    start_date = start_date or "未知时间"
    end_date = end_date or "未知时间"
    max_words = max_words or 500

    data = _load_data(dimension_data)
    summary = data.get("summary") or "暂无摘要数据"
    overview = data.get("overview") or {}

    try:
        overview_text = json.dumps(overview, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        overview_text = str(overview)
    if len(overview_text) > 2000:
        overview_text = overview_text[:2000] + "…"

    return f"""
你是专业的智慧园区运营报告生成专家，请根据下面的真实业务数据，生成「{dimension}」维度分析章节。

### 基础信息
- 公司名称：{company}
- 统计周期：{start_date} 至 {end_date}
- 分析维度：{dimension}

### 数据支撑
【数据摘要】
{summary}

【结构化指标】
{overview_text}

### 本维度分析重点
{focus}

### 生成规则
1. 字数严格控制在 {max_words} 字以内，不写空话、不重复。
2. 所有数字必须来自上面的数据；数据里没有的指标一律写「数据缺失」或「无相关数据」，严禁编造、估算、补全。
3. 单位必须与数据口径一致：电量用「千瓦时」，水量用「吨」，告警/事件用「次」，设备/表具用「台/个」。
4. 语言正式、客观、严谨，不使用口语化表达。
5. 统计周期以「{start_date} 至 {end_date}」为准，趋势分析的时间颗粒度要与之匹配。
6. 若【数据摘要】里出现「本期关注：…」，只写这些内容项对应的小节，未列出的内容项不要出现；标注「暂无对应数据接口」的项如实写「数据缺失」，不得用其他内容项替代。

### 报告结构（必须按以下编号和标题输出，不得增减章节）
#### 1.概述
按「（1）、」「（2）、」连续编号，说明统计周期、总体规模、数据构成。

#### 2.关键指标
按「（1）、」「（2）、」连续编号，逐项列出本维度的核心指标及数值、单位。只列数据里真实存在的指标。

#### 3.趋势分析
按「（1）、」「（2）、」连续编号，基于数据中的时间序列说明变化趋势；没有时间序列数据时写「无足够数据支撑趋势分析」。

#### 4.关键事件
按「（1）、」「（2）、」连续编号，说明本期值得关注的告警、异常或运行事件；没有相关数据时写「本期无相关事件数据」。

#### 5.原因剖析
按「（1）、」「（2）、」连续编号，从数据中能观察到的现象出发做归因；缺乏依据时写「无足够数据支撑原因分析」。

#### 6.模型洞察
按「（1）、」「（2）、」连续编号，给出基于本维度数据的结论与建议，建议要可执行。

### 输出格式要求
1. 直接输出章节正文，不要重复「{dimension}维度分析」这类一级标题。
2. 使用 Markdown 格式，关键数字可加粗。
3. 不得输出与本章节无关的内容。
"""
