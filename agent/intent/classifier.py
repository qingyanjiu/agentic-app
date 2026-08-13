import os
import asyncio
import logging
import numpy as np
from sentence_transformers import SentenceTransformer

# ============================================================
# 配置日志记录器
# 使用 logging 而不是 print，避免输出被缓冲
# ============================================================
logger = logging.getLogger(__name__)


# ============================================================
# 模型路径配置
# 优先从环境变量 INTENT_MODEL_PATH 读取本地挂载路径
# 容器启动示例：
#   docker run ... -e INTENT_MODEL_PATH=/root/models/BAAI_bge-small-zh-v1.5 ...
# 如果没有设置环境变量，默认从 HuggingFace 下载 BAAI/bge-small-zh-v1.5
# ============================================================
LOCAL_MODEL_PATH = os.getenv("INTENT_MODEL_PATH", "BAAI/bge-small-zh-v1.5")


# ============================================================
# 各意图判定阈值
# 取最高分意图后，只有最高分超过该意图的阈值才命中
# 否则判定为 other
# 安防语料刚开始不够多时，中心向量不稳定，可先调低
# ============================================================
PERSON_STATUS_THRESHOLD = 0.65
SECURITY_STATUS_THRESHOLD = 0.65
CANTEEN_STATUS_THRESHOLD = 0.65


# ============================================================
# 人员态势模块的示例语料库（按子类型分组）
# 每个 key 对应 extract_person_status_slots 里的 query_type：
#   location -> 人员当前位置
#   trace    -> 人员历史轨迹
#   realtime -> 实时人数 / 今日态势
#   enter    -> 进入人数
#   leave    -> 离开人数
#   flow     -> 人员流动趋势
#   structure-> 人员结构分布
#   abnormal -> 异常人员
#   count    -> 一般人数统计（兜底）
#
# 用途：
#   1. 所有示例的平均向量 = 人员态势意图中心（is_person_status）
#   2. 每个子类型的平均向量 = 子类型中心（classify_sub_type）
# 示例越多、覆盖越广，判断越准
# ============================================================
PERSON_STATUS_EXAMPLES = {
    # 位置查询（location）：询问某个人当前在哪
    "location": [
        "张三现在在哪",
        "李四在哪",
        "王五位置",
        "赵六现在在哪",
        "钱七在哪",
        "孙八在哪",
        "周九在哪",
        "吴十在哪",
        "查下张三位置",
        "张三在什么地方",
    ],

    # 轨迹查询（trace）：询问某个人去过哪、动向、行踪
    "trace": [
        "张三昨天轨迹",
        "李四去过哪里",
        "王五昨天去哪",
        "赵六动向",
        "钱七行踪",
        "孙八去过哪",
        "周九轨迹",
        "吴十昨天动向",
        "查下张三动向",
        "李四去过什么地方",
    ],

    # 实时人数（realtime）：询问当前/实时/在岗/在场人数
    "realtime": [
        "现在多少人",
        "实时人数多少",
        "园区现在多少人",
        "当前多少人",
        "今日态势",
        "在岗多少人",
        "在场多少人",
        "现在园区人数",
        "实时人数",
        "当前在场人数",
    ],

    # 进入人数（enter）：询问今天/当前进入了多少人
    "enter": [
        "今天进入多少人",
        "今天进来多少人",
        "今天入园多少人",
        "今天来了多少人",
        "今天进了多少人",
        "进入人数多少",
        "入园人数多少",
        "今天来多少人",
        "今天进多少人",
        "进来多少人",
    ],

    # 离开人数（leave）：询问今天/当前离开了多少人
    "leave": [
        "今天离开多少人",
        "今天出去多少人",
        "今天走了多少人",
        "今天出了多少人",
        "今天离园多少人",
        "离开人数多少",
        "出去多少人",
        "走了多少人",
        "今天出多少人",
        "离园人数多少",
    ],

    # 人员流动（flow）：询问人员流动趋势、进出趋势
    "flow": [
        "人员流动趋势",
        "进入人员趋势",
        "离开人员趋势",
        "人员流动情况",
        "人流趋势",
        "流入流出情况",
        "人员进出趋势",
        "园区人流趋势",
        "进入趋势",
        "离开趋势",
    ],

    # 人员结构（structure）：询问人员结构、分布、占比
    "structure": [
        "人员结构分布",
        "人员占比多少",
        "中通服多少人",
        "省公司多少人",
        "设计院多少人",
        "各部门人数",
        "人员组成",
        "人员分布",
        "其他人员多少",
        "结构分布",
    ],

    # 异常人员（abnormal）：询问陌生人、异常、可疑人员
    "abnormal": [
        "有陌生人吗",
        "异常人员有哪些",
        "可疑人员在哪",
        "黑名单有谁",
        "最近有异常吗",
        "陌生人进入",
        "异常人员多少",
        "可疑人员多少",
        "查到异常了吗",
        "异常人员",
    ],

    # 一般人数统计（count）：兜底的人数/态势统计
    "count": [
        "园区多少人",
        "今天多少人",
        "园区人数多少",
        "现在多少人",
        "今天园区人数",
        "园区共有多少人",
        "人员统计",
        "今日人数",
        "园区总人数",
        "查人数",
    ],
}

# ============================================================
# 安防态势模块的示例语料库（按子类型分组）
# 每个 key 对应 extract_security_status_slots 里的 event_type：
#   alarm_list   -> 告警列表
#   alarm_detail -> 告警详情
#   intrusion    -> 入侵 / 周界
#   patrol       -> 巡逻 / 巡更
#   video        -> 视频监控
#   access       -> 门禁 / 通行记录
#   fire         -> 火警 / 消防
#   device       -> 安防设备状态
#   abnormal     -> 异常事件 / 人员聚集
#
# 用途（与 PERSON_STATUS_EXAMPLES 相同）：
#   1. 所有示例的平均向量 = 安防态势意图中心（is_security_status）
#   2. 每个子类型的平均向量 = 子类型中心（classify_security_sub_type）
# 示例越多、覆盖越广，判断越准
# ============================================================
SECURITY_STATUS_EXAMPLES = {
    # 告警列表（alarm_list）：查询某时间段/今日的安防告警
    "alarm_list": [
        "查下今天的告警列表",
        "安防告警列表展示",
        "今天有哪些告警",
        "最近的安防告警",
        "告警记录有哪些",
        "今日告警汇总",
        "查一下这个月的告警",
        "有哪些未处理的告警",
        "告警列表拉一下",
        "展示今日安防告警",
    ],

    # 告警详情（alarm_detail）：看某条告警的具体信息
    "alarm_detail": [
        "查一下这条告警的详情",
        "告警详情是什么",
        "刚才那个告警具体是什么",
        "查看告警详细信息",
        "这条告警是什么原因",
    ],

    # 入侵/周界（intrusion）：翻越、闯入、越界
    "intrusion": [
        "有没有人翻越围墙",
        "周界报警",
        "有人非法闯入",
        "越界入侵告警",
        "有没有入侵事件",
        "周界入侵了没有",
        "非法翻越情况",
    ],

    # 巡逻（patrol）：巡逻任务、巡更记录
    "patrol": [
        "今天的巡逻任务",
        "巡更记录",
        "保安巡逻情况",
        "巡逻到哪了",
        "巡检点完成情况",
    ],

    # 视频监控（video）：调取画面、回放
    "video": [
        "调取大门口的监控画面",
        "看一下停车场视频",
        "回放监控",
        "摄像头画面",
        "视频监控在线情况",
    ],

    # 门禁通行（access）：刷卡、通行记录
    "access": [
        "门禁记录",
        "今天谁刷了门禁",
        "陌生人员刷闸",
        "出入通行记录",
        "大门通行情况",
    ],

    # 火警消防（fire）
    "fire": [
        "有没有火警",
        "烟雾报警",
        "消防告警",
        "火灾报警情况",
        "消防设备状态",
    ],

    # 设备状态（device）：在线率、故障
    "device": [
        "安防设备在线率",
        "摄像头离线了几个",
        "门禁设备状态",
        "报警主机运行情况",
        "设备故障告警",
    ],

    # 异常事件（abnormal）：人员聚集、行为异常
    "abnormal": [
        "人员聚集预警",
        "有没有异常事件",
        "行为异常",
        "群体性事件预警",
        "异常聚集",
    ],
}

# ============================================================
# 食堂管理模块的示例语料库（按子类型分组）
# 每个 key 对应 extract_canteen_status_slots 里的 event_type：
#   dish_rank    -> 本月菜品热度排行
#   week_menu    -> 本周菜谱
#   dining_count -> 就餐人数统计
#
# 用途（与 PERSON_STATUS_EXAMPLES 相同）：
#   1. 所有示例的平均向量 = 食堂管理意图中心（is_canteen_status）
#   2. 每个子类型的平均向量 = 子类型中心（classify_canteen_sub_type）
# 示例越多、覆盖越广，判断越准
# ============================================================
CANTEEN_STATUS_EXAMPLES = {
    # 本月菜品热度排行（dish_rank）：询问菜的热度/销量排行
    "dish_rank": [
        "本月菜品热度排行",
        "这个月哪个菜卖得好",
        "本月热门菜品",
        "菜品销量排行",
        "哪个菜最受欢迎",
        "食堂本月点单榜",
        "本月最火的菜",
        "菜品热度排名",
    ],

    # 本周菜谱（week_menu）：询问这周吃什么
    "week_menu": [
        "本周菜谱",
        "这周吃什么",
        "本周菜单",
        "这周食堂有什么菜",
        "本周午餐菜单",
        "看看这周的菜谱",
        "本周每天吃什么",
        "这周食堂菜单",
    ],

    # 就餐人数统计（dining_count）：询问多少人吃饭
    "dining_count": [
        "就餐人数统计",
        "今天多少人吃饭",
        "本周就餐人次",
        "食堂就餐人数",
        "本月用餐人数",
        "今天中午多少人吃饭",
        "本周有多少人就餐",
        "食堂人流量",
    ],
}
# ============================================================
# 人员态势小模型分类器
# 基于 sentence-transformers 的 embedding 相似度判断
#
# 注意：
#   SentenceTransformer 的加载和 encode 都是同步阻塞操作
#   不能在 asyncio 主事件循环中直接调用
#   必须通过 run_in_executor 放到线程池中执行
# ============================================================
class PersonStatusClassifier:
    def __init__(self):
        """
        初始化分类器
        在线程池中被调用，避免阻塞 Uvicorn 主事件循环
        """
        logger.info("[classifier] 开始加载意图识别模型...")

        # 加载本地或 HuggingFace 上的 sentence-transformer 模型
        self.model = SentenceTransformer(LOCAL_MODEL_PATH)

        logger.info("[classifier] 开始预计算示例向量...")

        # 1. 各子类型中心向量（用于判断 query_type）
        #    每个子类型取该组示例的平均向量并归一化
        self.sub_centers = {}
        all_vectors = []
        for sub_type, examples in PERSON_STATUS_EXAMPLES.items():
            vecs = self.model.encode(
                examples,
                convert_to_numpy=True,
                normalize_embeddings=True
            )
            all_vectors.append(vecs)
            center = np.mean(vecs, axis=0)
            self.sub_centers[sub_type] = center / np.linalg.norm(center)

        # 2. 整体中心向量（用于判断是否属于人员态势）
        #    与原有逻辑保持一致：全部示例的平均向量
        self.example_vectors = np.concatenate(all_vectors, axis=0)
        center = np.mean(self.example_vectors, axis=0)
        self.center = center / np.linalg.norm(center)
        # 3. 安防态势整体中心向量 + 子类型中心向量
        self.security_center = None
        self.security_sub_centers = {}
        sec_vectors = []
        for sub_type, examples in SECURITY_STATUS_EXAMPLES.items():
            vecs = self.model.encode(
                examples, convert_to_numpy=True, normalize_embeddings=True
            )
            sec_vectors.append(vecs)
            center = np.mean(vecs, axis=0)
            self.security_sub_centers[sub_type] = center / np.linalg.norm(center)

        if sec_vectors:
            all_sec = np.concatenate(sec_vectors, axis=0)
            center = np.mean(all_sec, axis=0)
            self.security_center = center / np.linalg.norm(center)

        # 4. 食堂管理整体中心向量 + 子类型中心向量
        self.canteen_center = None
        self.canteen_sub_centers = {}
        can_vectors = []
        for sub_type, examples in CANTEEN_STATUS_EXAMPLES.items():
            vecs = self.model.encode(
                examples, convert_to_numpy=True, normalize_embeddings=True
            )
            can_vectors.append(vecs)
            center = np.mean(vecs, axis=0)
            self.canteen_sub_centers[sub_type] = center / np.linalg.norm(center)

        if can_vectors:
            all_can = np.concatenate(can_vectors, axis=0)
            center = np.mean(all_can, axis=0)
            self.canteen_center = center / np.linalg.norm(center)


        logger.info("[classifier] 意图识别模型加载完成")

    def _encode(self, query: str) -> np.ndarray:
        """把用户输入编码成归一化的向量"""
        return self.model.encode(
            [query],
            convert_to_numpy=True,
            normalize_embeddings=True
        )[0]

    def is_person_status(self, query: str, threshold: float = 0.65) -> tuple:
        """
        判断用户输入是否属于人员态势意图
        在线程池中被调用，避免阻塞主事件循环

        :param query: 用户输入
        :param threshold: 相似度阈值，超过则判定为人员态势
        :return: (是否命中, 相似度分数)
        """
        query_vec = self._encode(query)

        # 计算用户输入与人员态势中心向量的余弦相似度
        # 因为已经归一化，点积就是余弦相似度
        score = float(np.dot(query_vec, self.center))

        logger.info(f"[classifier] query={query}, score={score:.4f}")

        # 返回是否超过阈值
        return score >= threshold, score
    def is_security_status(self, query: str, threshold: float = SECURITY_STATUS_THRESHOLD) -> tuple:
        """判断用户输入是否属于安防态势意图"""
        if self.security_center is None:
            return False, 0.0
        query_vec = self._encode(query)
        score = float(np.dot(query_vec, self.security_center))
        logger.info(f"[classifier] query={query}, security_score={score:.4f}")
        return score >= threshold, score


    def classify_top_intent(self, query: str) -> tuple:
        """
        多意图判定：一次编码，分别算两个意图中心相似度，
        取最高分；只有最高分超过该意图阈值才命中，否则 other
        """
        query_vec = self._encode(query)
        scores = {
            "person_status": float(np.dot(query_vec, self.center)),
            "security_status": float(np.dot(query_vec, self.security_center))
            if self.security_center is not None else -1.0,
            "canteen_status": float(np.dot(query_vec, self.canteen_center))
            if self.canteen_center is not None else -1.0,
        }
        best = max(scores, key=scores.get)
        best_score = scores[best]
        threshold = {
            "person_status": PERSON_STATUS_THRESHOLD,
            "security_status": SECURITY_STATUS_THRESHOLD,
            "canteen_status": CANTEEN_STATUS_THRESHOLD,
        }.get(best, PERSON_STATUS_THRESHOLD)
        if best_score >= threshold:
            return best, best_score
        return "other", best_score


    def classify_security_sub_type(self, query: str) -> tuple:
        """安防态势子类型判定，对称于 classify_sub_type"""
        query_vec = self._encode(query)
        best_type, best_score = None, -1.0
        for sub_type, center in self.security_sub_centers.items():
            score = float(np.dot(query_vec, center))
            if score > best_score:
                best_type, best_score = sub_type, score
        logger.info(f"[classifier] query={query}, security_sub_type={best_type}, score={best_score:.4f}")
        return best_type, best_score


    def classify_canteen_sub_type(self, query: str) -> tuple:
        """食堂管理子类型判定，对称于 classify_security_sub_type"""
        query_vec = self._encode(query)
        best_type, best_score = None, -1.0
        for sub_type, center in self.canteen_sub_centers.items():
            score = float(np.dot(query_vec, center))
            if score > best_score:
                best_type, best_score = sub_type, score
        logger.info(f"[classifier] query={query}, canteen_sub_type={best_type}, score={best_score:.4f}")
        return best_type, best_score


    def classify_sub_type(self, query: str) -> tuple:
        """
        判断人员态势的子类型（query_type）
        返回与用户输入最相似的子类型及其分数

        :param query: 用户输入
        :return: (query_type, 相似度分数)，例如 ("structure", 0.72)
        """
        query_vec = self._encode(query)

        best_type = None
        best_score = -1.0
        for sub_type, center in self.sub_centers.items():
            score = float(np.dot(query_vec, center))
            if score > best_score:
                best_type = sub_type
                best_score = score

        logger.info(
            f"[classifier] query={query}, sub_type={best_type}, score={best_score:.4f}"
        )

        return best_type, best_score


# ============================================================
# 全局分类器单例引用
# 保证整个进程只加载一次模型
# ============================================================
_classifier = None


async def get_classifier():
    """
    异步获取分类器单例

    使用 asyncio.run_in_executor 在线程池中初始化模型
    避免在 Uvicorn 主事件循环中同步加载模型，导致 WebSocket 卡住
    """
    global _classifier
    if _classifier is None:
        loop = asyncio.get_event_loop()
        # 在线程池中执行 PersonStatusClassifier.__init__()
        # 返回初始化好的实例
        _classifier = await loop.run_in_executor(None, PersonStatusClassifier)
    return _classifier


async def classify_intent(query: str) -> dict:
    """
    统一意图识别入口（人员态势 / 安防态势 / 其他）

    :param query: 用户输入
    :return: {"intent": "person_status" | "security_status" | "other", "score": 相似度}
    """
    # 获取分类器单例
    # 第一次调用时会在线程池中加载模型
    classifier = await get_classifier()

    # 在线程池中执行 encode 和相似度计算
    loop = asyncio.get_event_loop()
    intent, score = await loop.run_in_executor(
        None,
        classifier.classify_top_intent,
        query
    )

    logger.info(
        f"[classify_intent] query={query}, intent={intent}, score={score:.4f}"
    )

    return {
        "intent": intent,
        "score": score
    }


async def classify_sub_type(query: str) -> tuple:
    """
    异步判断人员态势的子类型（query_type）

    :param query: 用户输入
    :return: (query_type, 相似度分数)
    """
    classifier = await get_classifier()

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        classifier.classify_sub_type,
        query
    )


async def classify_security_sub_type(query: str) -> tuple:
    """
    异步判断安防态势的子类型（event_type）

    :param query: 用户输入
    :return: (event_type, 相似度分数)，例如 ("alarm_list", 0.72)
    """
    classifier = await get_classifier()

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        classifier.classify_security_sub_type,
        query
    )


async def classify_canteen_sub_type(query: str) -> tuple:
    """
    异步判断食堂管理的子类型（event_type）

    :param query: 用户输入
    :return: (event_type, 相似度分数)，例如 ("menu", 0.72)
    """
    classifier = await get_classifier()

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        classifier.classify_canteen_sub_type,
        query
    )
