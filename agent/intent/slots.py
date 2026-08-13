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
# 目前只支持告警列表（alarm_list）
# 后续如需扩展其他子类型（入侵/巡逻/视频/门禁/火警/设备/异常事件），
# 在 extract_security_status_slots 里加正则判断即可
# ============================================================
def extract_security_status_slots(query: str) -> dict:
    """
    抽取安防态势相关的 slot（当前只支持告警列表）

    event_type：固定为 alarm_list（告警列表）
    date：时间范围，由 parse_time_slot 解析
          取 date["start_time"] / date["end_time"]
          作为调 MCP 服务的参数 {startTime, endTime}

    注意：
      - parse_time_slot 对"最近几天"这类模糊时间会返回 time_type="vague"，
        start_time/end_time 为 None，此时需要先向用户确认时间范围
        （由 LangGraph 图的 vague_date 节点处理）
      - 对未来时间会返回 time_type="future"，直接提示不可查

    :param query: 用户输入
    :return: {"event_type": "alarm_list", "date": {"start_time": ..., "end_time": ..., "raw": ...}}
    """
    slots = {
        # 目前只做告警列表，后续扩展子类型时在此加正则判断
        "event_type": "alarm_list",
        # 时间范围：date["start_time"] / date["end_time"]
        # 对应 MCP 工具参数 {startTime, endTime}
        "date": parse_time_slot(query),
    }

    print(f"[security slots] query={query} => {slots}")
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