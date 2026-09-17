import re
from datetime import datetime, timedelta
from jionlp import parse_time

# ============================================================
# jionlp 预热
# 第一次调用 jionlp.parse_time 时会加载模型和词典
# 在这里预先调用一次，避免首次请求时耗时
# 预热失败不影响功能，后续有兜底逻辑
# ============================================================
def _warmup_jionlp():
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parse_time("今天", time_base=now)
    except Exception:
        # 预热失败时静默处理，不影响主流程
        pass

# 模块导入时自动执行预热
_warmup_jionlp()


# ============================================================
# 区域别名词典
# 把用户可能说的不同说法统一映射到标准区域名
# 例如用户说"A座"、"A楼"都映射到"A栋"
# ============================================================
AREA_DICT = {
    "园区": ["园区", "厂里", "公司", "单位", "办公楼"],
    "A栋": ["A栋", "A座", "A楼"],
    "B栋": ["B栋", "B座", "B楼"],
    "主楼": ["主楼", "主楼大厅"],
    "食堂": ["食堂", "餐厅"],
    "停车场": ["停车场", "车库"],
    "大门口": ["大门口", "正门", "入口", "门岗"],
}


# ============================================================
# 员工名单
# 用于准确匹配园区人员姓名
#
# 为什么不用正则：
#   中文人名结构复杂，用正则很容易把"一下人员"等词错当成人名
#   用名单匹配最稳定
#
# 使用方式：
#   小项目可以直接写死在代码里
#   大项目可以从数据库或配置文件动态加载
# ============================================================
EMPLOYEE_NAMES = [
    "张三",
    "李四",
    "王五",
    "赵六",
    "钱七",
    "孙八",
    "周九",
    "吴十"
]


def _parse_chinese_number(num_str: str) -> int:
    """
    简单中文数字转换，支持一到九十九
    也兼容阿拉伯数字
    """
    mapping = {
        "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10
    }

    # 阿拉伯数字
    if num_str.isdigit():
        return int(num_str)

    # 纯中文：十一、十二、二十、二十三等
    total = 0
    last = 0
    for ch in num_str:
        if ch in mapping:
            v = mapping[ch]
            if v == 10:
                if last == 0:
                    total = 10
                else:
                    total += last * 10
                    last = 0
            else:
                last = v
    total += last
    return total if total > 0 else 2


# 序数选择表达式：第一个 / 第1个 / 第 2 个
_ORDINAL_CHOICE_RE = re.compile(r"第\s*([0-9]+|[一二两三四五六七八九十]+)\s*个?")


def parse_ordinal_choice(query: str, max_index: int) -> int | None:
    """
    从用户回复中解析"序数选选项"的表达，供追问轮使用（通用，各域可复用）

    支持：第一个 / 第1个 / 第 2 个 / 就第三个吧 / 1 / 2（整句纯数字）
    不带「第」前缀的句子（如"近三天"）不会误命中。

    :param query: 用户回复
    :param max_index: 选项总数（1-based 上限）
    :return: 1-based 序号；解析不到或超出范围返回 None
    """
    q = query.strip()

    # 整句纯数字，如「1」「2」
    if q.isdigit():
        n = int(q)
        return n if 1 <= n <= max_index else None

    m = _ORDINAL_CHOICE_RE.search(q)
    if m:
        n = _parse_chinese_number(m.group(1))
        if 1 <= n <= max_index:
            return n

    return None


def parse_time_slot(query: str) -> dict:
    """
    解析用户输入中的时间描述
    
    先处理 jionlp 识别不了的口语化表达（这两天、本周等），
    再交给 jionlp 兜底。
    """
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # ============================================================
    # 1. 手工处理口语化时间
    # ============================================================

    # 近N天 / 最近N天 / 前N天 / 过去N天 / 这N天
    # 例如：近两天、近2天、最近四天、前3天、过去五天
    m = re.search(r"(近|最近|前|过去|这)(\d+|[一二两三四五六七八九十]+)(?:个)?天", query)
    if m:
        n = _parse_chinese_number(m.group(2))
        days_back = max(1, n - 1)
        start = (now - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "time_type": "span",
            "start_time": start.isoformat(),
            "end_time": now.isoformat(),
            "raw": f"近{n}天"
        }

    # 本周 / 这星期 / 这个星期 / 近一周 / 最近一周
    if re.search(r"(本|这|近|最近一)个?[星期周]", query):
        monday = today_start - timedelta(days=today_start.weekday())
        return {
            "time_type": "span",
            "start_time": monday.isoformat(),
            "end_time": now.isoformat(),
            "raw": "本周"
        }

    # 上周 / 上星期 / 上个星期
    if re.search(r"上(个)?[星期周]", query):
        last_monday = today_start - timedelta(days=today_start.weekday() + 7)
        last_sunday = last_monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
        return {
            "time_type": "span",
            "start_time": last_monday.isoformat(),
            "end_time": last_sunday.isoformat(),
            "raw": "上周"
        }

    # 本月 / 这个月 / 近一个月 / 最近一个月
    if re.search(r"(本|这|近|最近一)个?月", query):
        month_start = today_start.replace(day=1)
        return {
            "time_type": "span",
            "start_time": month_start.isoformat(),
            "end_time": now.isoformat(),
            "raw": "本月"
        }

    # 上个月 / 上月
    if re.search(r"上(个)?月", query):
        last_month_end = today_start.replace(day=1) - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return {
            "time_type": "span",
            "start_time": last_month_start.isoformat(),
            "end_time": last_month_end.replace(hour=23, minute=59, second=59).isoformat(),
            "raw": "上个月"
        }

    # 昨天
    if "昨天" in query:
        yesterday_start = today_start - timedelta(days=1)
        yesterday_end = today_start - timedelta(seconds=1)
        return {
            "time_type": "span",
            "start_time": yesterday_start.isoformat(),
            "end_time": yesterday_end.isoformat(),
            "raw": "昨天"
        }

    # 前天
    if "前天" in query:
        before_yesterday_start = today_start - timedelta(days=2)
        before_yesterday_end = today_start - timedelta(days=1, seconds=1)
        return {
            "time_type": "span",
            "start_time": before_yesterday_start.isoformat(),
            "end_time": before_yesterday_end.isoformat(),
            "raw": "前天"
        }

    # ============================================================
    # 1.5 模糊时间表达：需要向用户确认具体范围
    # ============================================================
    _VAGUE_TIME_PATTERNS = {
        r"(这几|最近几|近几|前几|过去几)[天]|这段(日子|时间)|近段时间|最近": {
            "options": ["近三天", "近一周", "近一个月"],
            "default": "近三天"
        }
    }
    for pattern, meta in _VAGUE_TIME_PATTERNS.items():
        if re.search(pattern, query):
            return {
                "time_type": "vague",
                "start_time": None,
                "end_time": None,
                "options": meta["options"],
                "raw": query
            }

    # 明天 / 后天 / 大后天（未来时间，人员态势无法查询）
    if any(k in query for k in ["明天", "后天", "大后天"]):
        return {
            "time_type": "future",
            "start_time": None,
            "end_time": None,
            "raw": query
        }

    # ============================================================
    # 2. jionlp 原生解析
    # ============================================================
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        res = parse_time(query, time_base=now_str)
        if res:
            time_info = res["time"]
            if res["type"] == "time_point":
                return {
                    "time_type": "point",
                    "start_time": time_info,
                    "end_time": time_info,
                    "raw": query
                }
            elif res["type"] == "time_span":
                return {
                    "time_type": "span",
                    "start_time": time_info[0],
                    "end_time": time_info[1],
                    "raw": query
                }
    except Exception:
        pass

    # ============================================================
    # 3. 兜底：默认返回今天
    # ============================================================
    return {
        "time_type": "span",
        "start_time": today_start.isoformat(),
        "end_time": now.isoformat(),
        "raw": "今天"
    }

def parse_area_slot(query: str) -> str:
    """
    从用户输入中抽取区域参数
    
    使用别名词典做统一映射
    例如用户说"A座"会映射成标准区域"A栋"
    
    :param query: 用户输入
    :return: 标准区域名，如"A栋"；未命中返回空字符串
    """
    for standard, aliases in AREA_DICT.items():
        for alias in aliases:
            if alias in query:
                return standard
    return ""


def extract_person_name(query: str) -> str:
    """
    从用户输入中抽取人名
    
    使用员工名单精确匹配，避免把"一下人员"等通用词错当成人名
    
    :param query: 用户输入
    :return: 匹配到的人名；未匹配到返回空字符串，触发追问
    """
    for name in EMPLOYEE_NAMES:
        if name in query:
            return name
    return ""
def extract_person_status_slots(query: str) -> dict:
    """
    统一抽取人员态势相关的所有 slot
    覆盖图中大屏口径：实时、进入、离开、流动、结构、位置、轨迹、异常、统计

    query_type 取值：
        location   -> 人员当前位置
        trace      -> 人员历史轨迹
        realtime   -> 实时人数 / 今日态势
        enter      -> 进入人数（口语：来了几个 / 进了多少 / 入园多少）
        leave      -> 离开人数（口语：走了几个 / 出了多少 / 离园多少）
        flow       -> 人员流动趋势（折线图）
        structure  -> 人员结构分布（环形图）
        abnormal   -> 异常 / 可疑 / 陌生人
        count      -> 一般人数统计（兜底）
    """
    slots = {
        "query_type": "count",
        "person_name": extract_person_name(query),
        "date": parse_time_slot(query),
        "area": parse_area_slot(query)
    }

    q = query

    # ============================================================
    # 1. 轨迹（最具体，先判断）
    # 关键词：轨迹、行踪、动向、去过哪儿、到过
    # ============================================================
    if re.search(r"轨迹|行踪|行动路线|动向|去过哪儿|去过哪里|去过.*地方|到过|去过", q):
        slots["query_type"] = "trace"

    # ============================================================
    # 2. 位置
    # 关键词：在哪、位置、找不到、去哪儿了、在什么地方
    # ============================================================
    elif re.search(r"在哪|在哪里|在.*地方|位置|找不到|去哪儿了|去哪里了|在什么地方|在.*区域", q):
        slots["query_type"] = "location"

    # ============================================================
    # 3. 异常人员
    # 关键词：异常、可疑、黑名单、陌生人、没登记
    # ============================================================
    elif re.search(r"异常人员|可疑人员|黑名单|陌生人|没登记|不明人员|异常|可疑", q):
        slots["query_type"] = "abnormal"

    # ============================================================
    # 4. 人员流动趋势（折线图）
    # 关键词：流动、趋势、折线、流入流出、人流量、进入人员趋势、离开人员趋势
    # 注意：放 enter/leave 前面，避免“进入人员趋势”被误判为 enter
    # ============================================================
    elif re.search(
        r"人员流动|流动趋势|流入流出|进出情况|人流量|人流|"
        r"进入.*趋势|离开.*趋势|进出.*趋势|趋势图|折线图", q
    ):
        slots["query_type"] = "flow"

    # ============================================================
    # 5. 进入人数
    # 覆盖：进入、进来、进了、来、入园 等动词 + 多少/几个/几人/人数
    # 示例：
    #   今天进入多少人、前两天园区进了多少人、来了几个人、入园人数多少
    # ============================================================
    elif re.search(
        r"进入人数|进来人数|入园人数|"
        r"进了多少人|来了多少人|入了多少人|进了几人|来了几人|"
        r"进入几个|进来几个|入园几个|来了几个|进了几个|"
        r"进入多少|进来多少|入园多少|来.*多少|进.*多少|入.*多少", q
    ):
        slots["query_type"] = "enter"

    # ============================================================
    # 6. 离开人数
    # 覆盖：离开、出去、出了、走、离园 等动词 + 多少/几个/几人/人数
    # 示例：
    #   今天离开多少人、出去了几个人、走了几个、离园人数
    # ============================================================
    elif re.search(
        r"离开人数|出去人数|离园人数|"
        r"出了多少人|走了多少人|离开几人|出去几人|走了几人|"
        r"出去几个|走了几个|离开几个|"
        r"离开多少|出去多少|走了多少|出.*多少|走.*多少", q
    ):
        slots["query_type"] = "leave"

    # ============================================================
    # 7. 人员结构分布（环形图）
    # 关键词：结构、分布、组成、构成、占比、饼图、环形图
    # 也包含图中具体类别：中通服和信科技、省公司、规划设计院
    # 注意：放 enter/leave 后面，避免“省公司进了多少人”被误判为 structure
    # ============================================================
    elif re.search(
        r"人员结构|人员分布|人员组成|人员构成|占比|饼图|环形图|"
        r"各部门|各公司|各单位|中通服|省公司|规划设计院|和信", q
    ):
        slots["query_type"] = "structure"

    # ============================================================
    # 8. 实时人数 / 今日态势
    # 关键词：实时、现在、当前、目前、在岗、在场、今日态势
    # ============================================================
    elif re.search(
        r"实时人数|现在多少人|当前人数|目前人数|今日态势|"
        r"在岗人数|在场人数|现在.*多少|当前.*多少|实时.*人数|"
        r"现场.*多少|园区.*多少.*人", q
    ):
        slots["query_type"] = "realtime"

    # ============================================================
    # 9. 兜底：一般人数统计 / 态势
    # ============================================================
    # 默认就是 count，无需再判断

    print(f"[slots] query={q} => {slots}")
    return slots


# ============================================================
# 安防态势 slot 抽取
# 当前支持：alarm_list / alarm_detail / patrol / device
# 后续如需扩展其他子类型（入侵/巡逻/视频/门禁/火警/设备/异常事件），
# 在 extract_security_status_slots 里加正则判断即可
# ============================================================

def extract_alarm_id(query: str) -> str:
    """
    从用户输入中抽取告警 ID
    支持：第3条告警、告警ID 123、告警 456、id 789 等
    """
    patterns = [
        r"(?:第\s*)(\d+)(?:\s*条(?:告警)?)?",
        r"告警(?:ID|id)?[\s:：]+(\d+)",
        r"告警\s+(\d+)",
        r"id[\s:：]+(\d+)",
        r"编号[\s:：]+(\d+)",
    ]
    for p in patterns:
        m = re.search(p, query)
        if m:
            return m.group(1)
    return ""


def extract_security_status_slots(query: str) -> dict:
    """
    抽取安防态势相关的 slot

    event_type：
      - alarm_list        告警列表（按时间区间查）
      - alarm_detail      告警详情（无单独工具，用 AI 巡查事件列表兜底）
      - patrol            巡查/巡逻任务统计
      - device            安防设备状态/详情
      - security_index    园区综合安全指数（无需时间）
      - ai_alert          AI 告警态势统计
      - inspection_trend  安全巡查趋势（当天分时段）
      - ai_inspection     AI 巡查事件列表
      - ai_overview       AI 告警总览（今日告警数/累计告警/算法类型数/识别准确率，无需时间）
      - ai_trend          AI 告警趋势（近 7 天 / 近 30 天，按时间区间查）
      - ai_alarm_list     分类告警明细列表（安防/管理/环境预警，按时间区间查）
    date：时间范围，由 parse_time_slot 解析
          取 date["start_time"] / date["end_time"]
          作为调 MCP 服务的参数 {startTime, endTime}

    注意：
      - parse_time_slot 对"最近几天"这类模糊时间会返回 time_type="vague"，
        start_time/end_time 为 None，此时需要先向用户确认时间范围
        （由 LangGraph 图的 vague_date 节点处理）
      - 对未来时间会返回 time_type="future"，直接提示不可查

    :param query: 用户输入
    :return: {"event_type": "...", "date": {...}, "alarm_id": "..."}
    """
    q = query
    slots = {
        "event_type": "alarm_list",
        "date": parse_time_slot(query),
        "alarm_id": extract_alarm_id(query),
    }

    # 1. 园区安全指数：无需时间范围，优先判断
    if re.search(
        r"安全指数|安全状况|安全.*得分|园区.*安全.*分|安全评分|安全系数|"
        r"安全水平|安全.*怎么样|园区安全吗",
        q,
    ):
        slots["event_type"] = "security_index"

    # 2. AI 告警总览：今日告警数、累计告警、算法类型数、识别准确率（无需时间）
    elif re.search(
        r"告警总览|AI告警概览|智能告警概览|今日告警数|今日告警.*多少|"
        r"累计告警|算法类型|识别准确率",
        q,
    ):
        slots["event_type"] = "ai_overview"

    # 3. AI 告警趋势：近 7 天 / 近 30 天告警走势
    elif re.search(
        r"告警趋势|预警趋势|AI告警走势|智能告警走势|告警.*走势|"
        r"近\s*[17７]\s*天.*告警|近\s*30\s*天.*告警",
        q,
    ):
        slots["event_type"] = "ai_trend"

    # 4. 分类告警明细：安防/管理/环境预警分类列表
    elif re.search(
        r"管理预警|环境预警|安防预警|分类告警|告警明细|预警明细|"
        r"预警.*分类.*列表|分类.*预警",
        q,
    ):
        slots["event_type"] = "ai_alarm_list"

    # 5. AI 告警态势：类别数量/占比统计
    elif re.search(
        r"AI告警|智能告警|告警分布|告警占比|告警统计|各类告警|"
        r"告警.*分类|告警.*数量|告警.*情况",
        q,
    ):
        slots["event_type"] = "ai_alert"

    # 6. 巡查趋势：分时段巡查情况
    elif re.search(
        r"巡查趋势|巡检趋势|巡查情况|巡查异常|巡检情况|巡查统计|"
        r"巡查.*时段|巡查.*高峰",
        q,
    ):
        slots["event_type"] = "inspection_trend"

    # 7. AI 巡查事件：智能巡检发现的异常
    elif re.search(
        r"AI巡查|智能巡检|智能巡查|巡检告警|巡查.*发现|巡检.*发现|"
        r"智能.*发现.*异常",
        q,
    ):
        slots["event_type"] = "ai_inspection"

    # 7. 告警详情：最具体，优先判断
    elif re.search(
        r"告警详情|这条告警|这个告警|那条告警|那个告警|告警详情是什么|"
        r"告警具体是什么|告警原因|告警信息|告警内容|详情是什么",
        q,
    ):
        slots["event_type"] = "alarm_detail"

    # 8. 巡查/巡逻任务
    elif re.search(
        r"巡查|巡逻|巡更|巡检|保安|巡逻任务|巡查任务|巡更记录|巡检点|"
        r"这周的?巡查任务|这周的?巡逻任务|本周巡查|本周巡逻|近期巡查|近期巡逻",
        q,
    ):
        slots["event_type"] = "patrol"

    # 9. 安防设备状态/详情
    elif re.search(
        r"设备状态|设备在线率|摄像头离线|门禁设备|报警主机|设备故障|"
        r"安防设备|设备健康|设备在线|设备离线|摄像头状态|门禁状态|"
        r"设备运行情况|设备运行状态|摄像头|监控设备|设备详情",
        q,
    ):
        slots["event_type"] = "device"

    print(f"[security slots] query={query} => {slots}")
    return slots


# ============================================================
# 车辆态势 slot 抽取
# 目前支持子类型（query_type）：
#   parking_space        -> 停车位统计
#   traffic_flow         -> 车流量统计
#   parking_structure    -> 停车结构
#   official_vehicle     -> 公车统计
#   parking_duration_rank-> 停车时长排名
#   count                -> 一般车辆统计（兜底）
#
# 注意：
#   - Java MCP 后端目前只实现了上述 5 个工具，没有单独的
#     "停车场监控" / "车辆通行记录" 工具；
#   - 用户问"停车场监控"时落到 parking_space，由停车位统计回答；
#   - 用户问"通行记录 / 进出记录"时落到 traffic_flow，由车流量统计回答。
# ============================================================

def extract_vehicle_status_slots(query: str) -> dict:
    """
    统一抽取车辆态势相关的所有 slot
    """
    slots = {
        "query_type": "count",
        "date": parse_time_slot(query),
        "area": parse_area_slot(query),
    }

    q = query

    # 1. 停车位统计（含停车场监控类问题兜底）
    if re.search(
        r"停车位|剩余车位|空车位|车位余量|车位统计|车位.*多少|剩余多少.*车位|还有.*车位|车位情况|"
        r"停车场监控|停车.*监控|车库监控|车位监控|监控.*停车场|监控.*车库|看.*停车场",
        q,
    ):
        slots["query_type"] = "parking_space"

    # 2. 车流量统计（含通行记录类问题兜底）
    elif re.search(
        r"车流量|车辆.*流量|进出车辆|通行车辆|车辆.*多少|进出.*统计|今天.*多少.*车|"
        r"通行记录|车辆.*记录|进出记录|过车记录|出入记录|车辆.*通行|通行.*查询|通行.*统计",
        q,
    ):
        slots["query_type"] = "traffic_flow"

    # 3. 停车结构
    elif re.search(
        r"停车结构|车辆结构|车型分布|车辆类型|车辆占比|停车场.*结构|车位类型",
        q,
    ):
        slots["query_type"] = "parking_structure"

    # 4. 公车统计
    elif re.search(
        r"公车|公务车|公务用车|单位车辆|公家车|公务.*统计|公车.*多少",
        q,
    ):
        slots["query_type"] = "official_vehicle"

    # 5. 停车时长排名
    elif re.search(
        r"停车时长|停车.*时间|停车.*排名|停最久|停最长|停车时长.*排行|停车时长.*统计",
        q,
    ):
        slots["query_type"] = "parking_duration_rank"

    # 6. 兜底：一般车辆统计
    # 默认 count，无需再判断

    print(f"[vehicle slots] query={q} => {slots}")
    return slots


# ============================================================
# 食堂管理 slot 抽取
# 目前支持子类型（event_type）：
#   dish_rank    -> 本月菜品热度排行
#   week_menu    -> 本周菜谱
#   dining_count -> 就餐人数统计
# 可选 slot：meal（餐次，用于"本周午餐菜单"这类说法）
# ============================================================

# 餐次别名词典：把用户各种说法统一映射到标准餐次
MEAL_DICT = {
    "早餐": ["早餐", "早饭", "早"],
    "午餐": ["午餐", "午饭", "中餐", "中午"],
    "晚餐": ["晚餐", "晚饭", "晚上"],
    "夜宵": ["夜宵", "宵夜"],
}


def parse_meal_slot(query: str) -> str:
    """
    从用户输入中抽取餐次（早餐/午餐/晚餐/夜宵）
    未命中返回空字符串
    """
    for meal, aliases in MEAL_DICT.items():
        for alias in aliases:
            if alias in query:
                return meal
    return ""


def extract_canteen_status_slots(query: str) -> dict:
    """
    抽取食堂管理相关的 slot

    返回结构：
      {
        "event_type": "dish_rank",        # 子类型
        "date": parse_time_slot(query),   # 时间范围 -> MCP 的 {startTime, endTime}
        "meal": "午餐",                    # 餐次（可能为空，仅菜谱查询需要）
      }

    注意：
      - event_type 用正则按"人数 -> 热度排行 -> 菜谱"顺序判定
      - parse_time_slot 对"本周/本月/上周/上个月"等已能直接解析成时间区间，
        所以 date 天然就有 {start_time, end_time}
      - 正则匹配不到时默认 week_menu（本周菜谱），保证流程不断
    """
    q = query
    slots = {
        "event_type": "week_menu",
        "date": parse_time_slot(query),
        "meal": parse_meal_slot(query),
    }

    # ============================================================
    # event_type 判定
    # ============================================================

    # 1. 就餐人数统计：最明确，优先判断
    if re.search(r"多少人|就餐人数|人次|用餐人数|吃饭的人|人流量|人多少|人数统计", q):
        slots["event_type"] = "dining_count"

    # 2. 本月菜品热度排行
    elif re.search(r"热度|排行|热门|销量|最受欢迎|卖得好|排名|点单榜", q):
        slots["event_type"] = "dish_rank"

    # 3. 兜底：本周菜谱 / 菜单
    elif re.search(r"菜谱|菜单|吃什么|菜品|有什么菜|菜", q):
        slots["event_type"] = "week_menu"

    print(f"[canteen slots] query={q} => {slots}")
    return slots


# ============================================================
# 信息发布 slot 抽取
# 目前支持子类型（event_type）：
#   info_view        -> 信息发布一览（发布设备统计、类型占比、设备列表）
#   broadcast_view   -> 广播一览（广播设备在线/离线/占用统计）
#   task_trend       -> 任务执行趋势
#   program_count    -> 节目数量趋势
#   info_equip       -> 信息发布设备明细
#   broadcast_equip  -> 广播设备明细
#
# 可选 slot：device_type（设备类型，用于区分信息发布/广播设备）
# ============================================================

# 设备类型别名词典：把用户各种说法统一映射到标准类型
def extract_information_status_slots(query: str) -> dict:
    """
    抽取信息发布相关的 slot

    返回结构：
      {
        "event_type": "info_view",        # 子类型
        "date": parse_time_slot(query),   # 时间范围 -> MCP 的 {startTime, endTime}
      }

    注意：
      - event_type 用正则按"设备明细 -> 趋势/数量 -> 一览/统计"顺序判定
      - 信息发布和广播在文档里是两个并列子模块，这里统一归为信息发布意图下
      - 正则匹配不到时默认 info_view（信息发布一览），保证流程不断
    """
    q = query
    slots = {
        "event_type": "info_view",
        "date": parse_time_slot(query),
    }

    # ============================================================
    # event_type 判定：按特异性从高到低
    # ============================================================

    # 1. 广播设备明细
    if re.search(
        r"广播设备(明细|列表|详情|信息|状态)|广播终端|查看广播设备|广播设备.*怎么",
        q,
    ):
        slots["event_type"] = "broadcast_equip"

    # 2. 信息发布设备明细
    elif re.search(
        r"信息发布设备(明细|列表|详情|信息|状态)|信息发布终端|信息屏|发布屏|"
        r"查看信息发布设备|信息发布设备.*怎么|信息发布屏",
        q,
    ):
        slots["event_type"] = "info_equip"

    # 3. 任务执行趋势
    elif re.search(
        r"任务.*趋势|任务执行.*趋势|发布任务.*趋势|任务量.*趋势|任务执行量|任务趋势",
        q,
    ):
        slots["event_type"] = "task_trend"

    # 4. 节目数量趋势
    elif re.search(
        r"节目.*数量|节目数.*趋势|节目.*趋势|发布节目|节目数量|节目数",
        q,
    ):
        slots["event_type"] = "program_count"

    # 5. 广播一览
    elif re.search(
        r"广播.*一览|广播.*统计|广播.*状态|广播设备.*统计|广播概况|广播.*占比|广播在线",
        q,
    ):
        slots["event_type"] = "broadcast_view"

    # 6. 信息发布一览（兜底）
    # 关键词：信息发布、发布统计、发布设备、类型占比、发布一览
    elif re.search(
        r"信息发布|发布.*统计|发布.*一览|发布设备|类型占比|信息发布.*状态|发布.*概览",
        q,
    ):
        slots["event_type"] = "info_view"

    print(f"[information slots] query={q} => {slots}")
    return slots

# ============================================================
# 能源态势 slot 抽取
# 目前支持子类型（query_type）：
#   overall_energy      -> 总体能耗（电/水年度累计 + 今日用量）
#   metering_equipment  -> 表具设备（电表/水表数量）
#   electricity_rank    -> 用电排名
#   water_rank          -> 用水排名
#   device_status       -> 能耗设备在线/离线状态
#   realtime_electricity-> 实时用电（今日 vs 昨日曲线）
#   realtime_water      -> 实时用水（今日 vs 昨日曲线）
#   count               -> 一般能源统计（兜底）
# ============================================================

def extract_energy_status_slots(query: str) -> dict:
    """
    统一抽取能源态势相关的所有 slot
    """
    slots = {
        "query_type": "count",
        "date": parse_time_slot(query),
    }

    q = query

    # 1. 表具设备
    if re.search(
        r"表具|电表|水表|计量.*设备|计量表|智能表|表计",
        q,
    ):
        slots["query_type"] = "metering_equipment"

    # 2. 设备状态（在线/离线）
    elif re.search(
        r"能耗.*设备.*状态|能耗.*在线|能耗.*离线|能源.*设备|设备.*状态",
        q,
    ):
        slots["query_type"] = "device_status"

    # 3. 实时用电
    elif re.search(
        r"实时用电|用电趋势|用电曲线|今日用电|今天用电|现在用电|用电.*走势|"
        r"电.*趋势|电.*曲线",
        q,
    ):
        slots["query_type"] = "realtime_electricity"

    # 4. 实时用水
    elif re.search(
        r"实时用水|用水趋势|用水曲线|今日用水|今天用水|现在用水|用水.*走势|"
        r"水.*趋势|水.*曲线",
        q,
    ):
        slots["query_type"] = "realtime_water"

    # 5. 用电排名
    elif re.search(
        r"用电排名|耗电排名|用电.*排行|哪个.*用电最多|哪个.*耗电最多|耗电.*排行|"
        r"用电量.*排名|用电.*榜单",
        q,
    ):
        slots["query_type"] = "electricity_rank"

    # 6. 用水排名
    elif re.search(
        r"用水排名|耗水排名|用水.*排行|哪个.*用水最多|哪个.*耗水最多|耗水.*排行|"
        r"用水量.*排名|用水.*榜单",
        q,
    ):
        slots["query_type"] = "water_rank"

    # 7. 总体能耗（兜底中的高优先级）
    elif re.search(
        r"总体能耗|总能耗|能耗统计|用电.*统计|用水.*统计|年度.*能耗|今日.*能耗|"
        r"电.*统计|水.*统计|能源.*统计|能源.*概览|能耗.*概览|用电.*总量|用水.*总量|"
        r"今天用.*电|今天用.*水|用电情况|用水情况",
        q,
    ):
        slots["query_type"] = "overall_energy"

    # 8. 兜底：一般能源统计
    # 默认 count，无需再判断

    print(f"[energy slots] query={q} => {slots}")
    return slots


# ============================================================
# 设备态势 slot 抽取
# 目前支持子类型（query_type）：
#   equip_class       -> 设备分类数量占比
#   category_health   -> 各类别健康度（评分/在线率/维保率/寿命）
#   month_maintenance -> 月度维修趋势（可选 yyyy-MM）
#   month_repair      -> 月度报修趋势（可选 yyyy-MM）
#   statis_region     -> 分区域设备统计
#   anfang_online     -> 安防设备在线率
#   gb_online         -> 广播设备在线率
#   mj_online         -> 门禁设备在线率
#   count             -> 一般设备统计（兜底）
# ============================================================

def extract_device_status_slots(query: str) -> dict:
    """
    统一抽取设备态势相关的所有 slot
    """
    slots = {
        "query_type": "count",
        "date": parse_time_slot(query),
    }

    q = query

    # 1. 门禁在线率（优先于安防，避免"门禁"被"安防监控"泛化命中）
    if re.search(
        r"门禁.*在线|门禁.*离线|门禁.*率|门禁",
        q,
    ):
        slots["query_type"] = "mj_online"

    # 2. 广播在线率
    elif re.search(
        r"广播.*在线|广播.*离线|广播.*率|广播设备",
        q,
    ):
        slots["query_type"] = "gb_online"

    # 3. 安防在线率
    elif re.search(
        r"安防.*在线|安防.*离线|安防.*率|安防设备|监控.*在线|监控.*离线|监控.*率|"
        r"摄像头.*在线|摄像头.*离线",
        q,
    ):
        slots["query_type"] = "anfang_online"

    # 4. 月度报修趋势
    elif re.search(
        r"报修.*趋势|报修.*统计|报修量|报修数|月度.*报修|每月.*报修|本月.*报修|上月.*报修|"
        r"报修.*走势|报修.*曲线|报修.*情况",
        q,
    ):
        slots["query_type"] = "month_repair"

    # 5. 月度维修/维保趋势
    elif re.search(
        r"维修.*趋势|维修.*统计|维修量|维修数|维保.*趋势|维保.*统计|维保率|月度.*维修|"
        r"每月.*维修|本月.*维修|上月.*维修|维修.*走势|维修.*曲线|维修.*情况|保养.*趋势",
        q,
    ):
        slots["query_type"] = "month_maintenance"

    # 6. 类别健康度
    elif re.search(
        r"健康度|健康.*评分|设备.*健康|类别.*健康|在线率.*维保|寿命|"
        r"消防.*健康|空调.*健康|能耗.*健康",
        q,
    ):
        slots["query_type"] = "category_health"

    # 7. 分区域设备统计
    elif re.search(
        r"分区域|区域.*设备|各区.*设备|区域.*统计|楼栋.*设备|楼层.*设备|"
        r"区域.*数量|分布",
        q,
    ):
        slots["query_type"] = "statis_region"

    # 8. 设备分类占比
    elif re.search(
        r"设备.*分类|分类.*占比|设备.*占比|类别.*占比|设备.*数量|各类.*设备|"
        r"设备类型|设备种类|占比.*统计",
        q,
    ):
        slots["query_type"] = "equip_class"

    # 9. 兜底：一般设备统计
    # 默认 count，无需再判断

    print(f"[device slots] query={q} => {slots}")
    return slots


# ============================================================
# 综合态势总览 slot 抽取
# 目前支持子类型（query_type）：
#   basic_info    -> 园区基本信息（面积 / IoT设备总数 / 园区概况）
#   device_health -> 设备健康度总览（健康分/在线率/维修率/寿命率）
#
# 约定（docs/开发计划.md §2.3）：
#   总览只保留这两个独占子类型，走 /overview/* 自己的接口；
#   人车/能耗/会议/安全指数问法由 person/vehicle/energy/meeting/security 承接
# ============================================================

def extract_compositive_overview_slots(query: str) -> dict:
    """
    统一抽取综合态势总览相关的所有 slot
    """
    slots = {
        "query_type": "basic_info",
        "date": parse_time_slot(query),
    }

    q = query

    # 1. 设备健康度总览
    #    只收"健康度/健康分"总览口径；类别健康度问法
    #    （如"消防设备健康度"）在顶层意图就被分到 device_status，到不了这里
    if re.search(
        r"健康度|健康分|健康.*情况|健康.*状况|健康.*评分",
        q,
    ):
        slots["query_type"] = "device_health"

    # 2. 兜底：园区基本信息（面积 / IoT设备总数 / 园区概况）
    #    默认 basic_info，无需再判断

    print(f"[compositive overview slots] query={q} => {slots}")
    return slots


# ============================================================
# 消防态势 slot 抽取
# 目前支持子类型（query_type）：
#   fire_assets     -> 消防设备台账（状态/压力液位/电量/倾角）
#   fire_alarm_num  -> 设备告警统计（火警/故障/隐患/漏报/离人）
#   fire_alarm_list -> 实时消防告警列表
#   month_repair    -> 月度报修趋势（支持本月/上月/X月）
#   count           -> 一般消防统计（兜底）
# ============================================================

def extract_emergency_fire_slots(query: str) -> dict:
    """
    统一抽取消防态势相关的所有 slot
    """
    slots = {
        "query_type": "count",
        "date": parse_time_slot(query),
    }

    q = query

    # 1. 月度报修（报修/维修，优先级最高，避免被"告警"等词误抢）
    if re.search(
        r"报修|维修|检修|维保",
        q,
    ):
        slots["query_type"] = "month_repair"

    # 2. 实时告警
    elif re.search(
        r"实时告警|告警列表|实时消防告警|当前告警|最新告警|报警列表|"
        r"消防告警列表|消防报警记录|最近的火警|现在有什么告警|告警信息|"
        r"实时报警|报警记录",
        q,
    ):
        slots["query_type"] = "fire_alarm_list"

    # 3. 设备告警统计
    elif re.search(
        r"告警统计|火警数|火警数量|故障数|设备故障|隐患数|火灾隐患|误报数|"
        r"漏报数|离人告警|离人数|报警统计|消防报警统计|告警分类|告警.*统计|"
        r"有多少.*火警|有多少.*故障|有多少.*隐患",
        q,
    ):
        slots["query_type"] = "fire_alarm_num"

    # 4. 消防设备台账
    elif re.search(
        r"灭火器|消防栓|烟感|水压|液位|电量|倾角|消防设备|消防.*台账|"
        r"设备台账|设备明细|设备状态|消防.*设备|台账",
        q,
    ):
        slots["query_type"] = "fire_assets"

    # 5. 兜底：一般消防统计
    # 默认 count，无需再判断

    print(f"[fire slots] query={q} => {slots}")
    return slots


# ============================================================
# 周界态势（孪生周界）slot 抽取
# 目前支持子类型（query_type）：
#   key_metrics           -> 关键指标（在线/离线防区设备数、今日告警数）
#   area_overview         -> 防区一览（防区列表与布防状态）
#   perimeter_alarm_stats -> 周界告警统计（按区域/时段统计与小时曲线）
#   alarm_overview        -> 告警一览（周界告警列表，点击看抓拍照片）
#   count                 -> 一般周界查询（兜底）
# ============================================================

def extract_emergency_perimeter_slots(query: str) -> dict:
    """
    统一抽取周界态势相关的所有 slot
    """
    slots = {
        "query_type": "count",
        "date": parse_time_slot(query),
    }

    q = query

    # 1. 告警一览（列表/实时口径，优先于统计，避免"告警列表"被统计误抢）
    if re.search(
        r"告警一览|告警列表|实时周界|实时告警|最新告警|当前告警|报警列表|"
        r"报警记录|告警记录|告警信息|(最新|最近).{0,2}(周界|围栏)告警|"
        r"现在有什么告警|有什么周界告警|告警抓拍|入侵记录|翻越.*记录",
        q,
    ):
        slots["query_type"] = "alarm_overview"

    # 2. 关键指标（在线/离线防区设备数、今日告警数，
    #    放在统计之前，避免"今日告警数"被"告警数"类统计正则误抢）
    elif re.search(
        r"关键指标|防区.*(在线|离线)|(在线|离线).*防区|周界.*(在线|离线)|"
        r"今日告警数|今日报警数|今日.*告警数|在线防区|离线防区",
        q,
    ):
        slots["query_type"] = "key_metrics"

    # 3. 周界告警统计（按区域/时段统计与小时曲线）
    elif re.search(
        r"告警统计|报警统计|告警.*统计|告警.*分布|告警.*趋势|告警.*时段|"
        r"时段.*分布|小时.*曲线|告警.*曲线|按区域|告警分类|告警.*分析|"
        r"有多少.*告警|告警.*情况",
        q,
    ):
        slots["query_type"] = "perimeter_alarm_stats"

    # 4. 防区一览（防区列表与布防状态）
    elif re.search(
        r"防区|布防|撤防|围栏|围界|周界",
        q,
    ):
        slots["query_type"] = "area_overview"

    # 5. 兜底：一般周界查询
    # 默认 count，无需再判断

    print(f"[perimeter slots] query={q} => {slots}")
    return slots


# ============================================================
# 孪生巡检 slot 抽取
# 目前支持子类型（query_type）：
#   today_inspection             -> 今日巡检（任务数/点位/完成率 + 分时段图表）
#   today_tasks                  -> 今日任务列表（人员/类型/状态/班组/时间）
#   inspection_statistics        -> 巡检统计（平均时长/点位/隐患数，近1月/3月/1年）
#   inspection_execution_status  -> 巡检执行状态（按人巡检正常/异常）
#   count                        -> 一般巡检查询（兜底）
# ============================================================

def extract_twins_inspection_slots(query: str) -> dict:
    """
    统一抽取孪生巡检相关的所有 slot
    """
    slots = {
        "query_type": "count",
        "date": parse_time_slot(query),
    }

    q = query

    # 1. 今日巡检总览（任务数/点位/完成率口径，
    #    放在任务列表之前，避免"今日任务数/多少巡检任务"被任务列表正则误抢；
    #    "今日巡检"后跟"任务"时不算总览，让位给任务列表）
    if re.search(
        r"任务数|完成率|巡检完成|今日巡检(?!任务)|巡检总览|巡检概况|"
        r"多少.{0,4}巡检任务|多少.{0,4}点位|巡检点位",
        q,
    ):
        slots["query_type"] = "today_inspection"

    # 2. 今日任务列表（人员/类型/状态/班组/时间口径）
    elif re.search(
        r"任务列表|任务清单|巡检任务|任务安排|巡检安排|巡检班组|"
        r"巡检人员|谁在巡检",
        q,
    ):
        slots["query_type"] = "today_tasks"

    # 3. 巡检执行状态（按人巡检正常/异常口径，先于统计，
    #    避免"各人巡检正常异常统计"被统计误抢）
    elif re.search(
        r"执行状态|巡检执行|按人|巡检员|巡检正常|巡检异常|"
        r"正常.{0,4}异常|异常.{0,4}正常|每人.{0,4}巡检",
        q,
    ):
        slots["query_type"] = "inspection_execution_status"

    # 4. 巡检统计（平均时长/点位/隐患数，近1月/3月/1年）
    elif re.search(
        r"巡检统计|巡检.{0,4}统计|平均.{0,4}时长|巡检时长|隐患|"
        r"近一月|近三月|近一年",
        q,
    ):
        slots["query_type"] = "inspection_statistics"

    # 5. 兜底：一般巡检查询
    # 默认 count，无需再判断

    print(f"[inspection slots] query={q} => {slots}")
    return slots


# ============================================================
# 会议管理 slot 抽取
# 目前支持子类型（event_type）：
#   meeting_statistics      -> 会议统计（预约数/平均时长/环比）
#   high_freq_meeting_rooms -> 高频会议室 TOP5
#   meeting_room_overview   -> 会议室一览
#   number_of_meetings      -> 会议数量趋势
#   meeting_room_status     -> 会议室使用/空闲状态
#   room_meet_list_by_day   -> 当日会议安排
#   count                   -> 一般会议统计（兜底）
# ============================================================

def extract_meeting_status_slots(query: str) -> dict:
    """
    统一抽取会议管理相关的所有 slot
    """
    slots = {
        "event_type": "count",
        "date": parse_time_slot(query),
    }

    q = query

    # 1. 当日会议安排
    if re.search(
        r"当日.*会议|今天.*会议|今日.*会议|会议.*安排|会议.*列表|有什么会|"
        r"今天.*会|今日.*会|会议.*日程|会议.*计划",
        q,
    ):
        slots["event_type"] = "room_meet_list_by_day"

    # 2. 高频会议室
    elif re.search(
        r"高频会议室|会议室.*排行|会议室.*排名|哪个会议室.*最多|"
        r"最热门.*会议室|会议室使用.*排行",
        q,
    ):
        slots["event_type"] = "high_freq_meeting_rooms"

    # 3. 会议室状态
    elif re.search(
        r"会议室.*状态|会议室.*空闲|会议室.*占用|哪些会议室.*可用|"
        r"会议室.*使用|空闲.*会议室",
        q,
    ):
        slots["event_type"] = "meeting_room_status"

    # 4. 会议室一览
    elif re.search(
        r"会议室.*一览|会议室.*情况|会议室.*概览|会议室.*分布|"
        r"会议室.*统计",
        q,
    ):
        slots["event_type"] = "meeting_room_overview"

    # 5. 会议数量
    elif re.search(
        r"会议.*数量|多少.*会议|会议.*趋势|会议.*统计|会议量|"
        r"会议.*走势|会议.*情况",
        q,
    ):
        slots["event_type"] = "number_of_meetings"

    # 6. 会议统计
    elif re.search(
        r"会议统计|预约.*会议|会议.*预约|平均.*会议.*时长|会议.*时长|"
        r"会议.*概览|会议.*总览",
        q,
    ):
        slots["event_type"] = "meeting_statistics"

    # 7. 兜底：一般会议统计
    # 默认 count，无需再判断

    print(f"[meeting slots] query={q} => {slots}")
    return slots


# ============================================================
# 设备查询模块 slot 抽取
# 与"设备态势"（/services/device，统计口径）区分：
#   设备查询是"台账口径"——查设备列表 / 查某台设备详情
#
# 目前支持子类型（query_type）：
#   device_list   -> 设备列表（可按设备类型、区域/楼栋/楼层筛选）
#   device_detail -> 设备详情（需要设备名称或编号定位到具体设备）
#
# 口径是**资产库**（Java 侧 /mcp/devicequery 的 device_query:listDevice /
# device_query:getDeviceDetail，查平台纳管的设备资产），不是厂商实时设备：
#   1. 设备类型码是 syncSource（八类，见下），它是**可选**筛选条件——
#      列表接口所有筛选参数都可选，不传就是全部设备
#   2. 只有详情必须有 device_keyword（设备名称或编号）：
#      详情接口只认内部 id，得先拿名称/编号去列表里把它搜出来，缺失时追问
#   3. 位置（area）后端只认 spaceId，这里的位置名由 graph 侧拉回列表后
#      按 spaceName 本地过滤
# ============================================================

# 设备类型别名词典
# key 就是资产库的 syncSource 取值（取值见 /deviceInfo/list 接口文档）：
#   0 门禁 1 道闸 2 梯控 3 监控 4 入侵报警 5 广播 6 水表 7 电表
#
# 注意：消防（灭火器/烟感）归 emergency_fire 域，网络设备/空调在资产库里
# 没有对应类型，不要收进词典。命中不了就不落码——按"全部设备"查即可。
DEVICE_TYPE_DICT = {
    "0": ["门禁设备", "门禁", "闸机", "门禁控制器", "门禁通道"],
    "1": ["道闸设备", "道闸", "车闸", "车牌识别闸机"],
    "2": ["梯控设备", "梯控", "电梯控制", "电梯"],
    "3": ["监控设备", "监控", "摄像头", "摄像机", "枪机", "球机", "半球", "视频监控"],
    "4": ["入侵报警设备", "入侵报警", "周界报警", "报警设备", "红外报警", "报警"],
    "5": ["广播设备", "广播", "音箱", "喇叭", "广播终端"],
    "6": ["水表", "智能水表"],
    "7": ["电表", "智能电表"],
}


def extract_device_type(query: str) -> str:
    """
    从用户输入中抽取设备类型（资产库口径的 syncSource）

    使用别名词典做统一映射，例如"摄像头"映射成 syncSource="3"。

    多个别名同时命中时取**最长**的那个：查"车牌识别闸机"要判成道闸，
    不能被里面的"闸机"抢成门禁；"入侵报警"同理不能被"报警"抢走。
    长度相同时按词典顺序取先出现的。

    :param query: 用户输入
    :return: syncSource 取值（0门禁 1道闸 2梯控 3监控 4入侵报警 5广播 6水表 7电表）；
             未命中返回空字符串
    """
    matched_code, matched_len = "", 0
    for code, aliases in DEVICE_TYPE_DICT.items():
        for alias in aliases:
            if len(alias) > matched_len and alias in query:
                matched_code, matched_len = code, len(alias)
    return matched_code


def parse_floor_slot(query: str) -> str:
    """
    从用户输入中抽取楼层（设备查询特有：设备台账按楼层定位很常见）

    例如："3楼"、"三楼"、"3层"、"3F" -> "3楼"

    :param query: 用户输入
    :return: 楼层描述，如"3楼"；未命中返回空字符串
    """
    m = re.search(r"([0-9]+|[一二三四五六七八九十]+)\s*(?:楼|层|[fF])", query)
    if not m:
        return ""
    # 中文数字统一成阿拉伯数字，便于后端按楼层过滤（"三楼" 与 "3楼" 命中同一条数据）
    return f"{_parse_chinese_number(m.group(1))}楼"


# 设备编号形态（两种，取先命中的）：
#   ① 多段带分隔符的资产编号：CY-HIK-JK-001-0001 / DEV-A-01
#   ② 字母 + 数字的短编码：MH-001 / CAM102 / DEV_01
DEVICE_CODE_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+|[A-Za-z]{1,8}[-_]?\d{1,6})"
)


def extract_device_keyword(query: str) -> str:
    """
    从用户输入中抽取设备名称/编码关键字

    只做高置信度抽取（编码、引号包裹、显式"名称是/叫"句式），
    抽不到就返回空串，由详情查询的追问逻辑兜底——
    宁可追问一句，也不要把"设备"、"详情"这类通用词当成设备名。

    :param query: 用户输入
    :return: 设备名称/编码关键字；未命中返回空字符串
    """
    # 1. 设备编号：资产库的编号形如 CY-HIK-JK-001-0001（多段带分隔符），
    #    也有 MH-001 / CAM102 这类短编码。两种都要整串抽出来——
    #    只认"字母+数字"会把 CY-HIK-JK-001-0001 截成 JK-001
    m = DEVICE_CODE_RE.search(query)
    if m:
        return m.group(0)

    # 2. 引号包裹的名称
    m = re.search(r"[“\"'‘]([^”\"'’]{2,20})[”\"'’]", query)
    if m:
        return m.group(1).strip()

    # 3. 显式句式：名称是X / 编码为X / 叫X的
    m = re.search(
        r"(?:名称|名字|编号|编码)(?:是|为|叫|:|：)?\s*([\w一-龥\-]{2,20})",
        query,
    ) or re.search(r"叫([\w一-龥\-]{2,20})的", query)
    if m:
        keyword = m.group(1)
        # 去掉被一起捕获的尾巴（"的详情"、"的信息"…）
        keyword = re.sub(r"(的)?(详情|信息|情况|数据|列表|资料).*$", "", keyword)
        keyword = keyword.strip("的了 ")
        if len(keyword) >= 2:
            return keyword

    return ""


def extract_device_query_slots(query: str) -> dict:
    """
    统一抽取设备查询相关的所有 slot

    query_type：
      - device_list   设备列表（可按设备类型/区域/楼层筛选）
      - device_detail 设备详情（需要 device_keyword 定位设备）
    device_type：设备类型码 syncSource，0门禁 1道闸 2梯控 3监控 4入侵报警 5广播
                 6水表 7电表（可选筛选：资产库列表接口不传就是全部类型）
    area：区域/楼栋（复用 AREA_DICT），与楼层拼接，如"A栋3楼"；
          注意后端列表接口只认 spaceId，这里是位置名，由 graph 侧转成本地过滤
    device_keyword：设备名称/编号关键字（详情必填；列表可选，用于本地模糊匹配）

    :param query: 用户输入
    :return: {"query_type": "...", "device_type": "...", "area": "...", "device_keyword": "..."}
    """
    q = query

    # 区域 / 楼栋 + 楼层，例如"A栋3楼" / "食堂" / "3楼"
    area = parse_area_slot(query)
    floor = parse_floor_slot(query)
    if floor and floor not in area:
        area = f"{area}{floor}"

    slots = {
        "query_type": "device_list",
        "device_type": extract_device_type(query),
        "area": area,
        "device_keyword": extract_device_keyword(query),
    }

    # 1. 设备详情：问某台设备的具体信息
    #    "设备详情/设备档案/这台设备的..." 归详情，
    #    "设备列表/有哪些设备" 归列表（默认值，无需判断）
    if re.search(
        r"详情|详细|档案|具体信息|设备信息|这台设备|该设备|这台|那台|"
        r"是什么设备|设备的?参数|规格",
        q,
    ):
        slots["query_type"] = "device_detail"

    # 1.1 已经用名称/编码定位到某台设备，再问"信息/资料/情况"，
    #     也是详情查询（避免"XX设备的信息"被当成列表口径）
    elif slots["device_keyword"] and re.search(r"信息|资料|情况|参数|状态", q):
        slots["query_type"] = "device_detail"

    # 2. 兜底：设备列表
    #    默认 device_list，无需再判断

    print(f"[device query slots] query={q} => {slots}")
    return slots
