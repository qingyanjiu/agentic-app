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
    统一意图识别入口（当前只判断人员态势）

    :param query: 用户输入
    :return: {"intent": "person_status" | "other", "score": 相似度}
    """
    # 获取分类器单例
    # 第一次调用时会在线程池中加载模型
    classifier = await get_classifier()

    # 在线程池中执行 encode 和相似度计算
    loop = asyncio.get_event_loop()
    is_ps, score = await loop.run_in_executor(
        None,
        classifier.is_person_status,
        query
    )

    logger.info(
        f"[classify_intent] query={query}, "
        f"intent={'person_status' if is_ps else 'other'}, "
        f"score={score:.4f}"
    )

    return {
        "intent": "person_status" if is_ps else "other",
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
