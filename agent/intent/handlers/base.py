import re
from abc import ABC, abstractmethod

# ============================================================
# 意图处理器基类
# 所有具体模块的 handler 都需要继承这个类
# 统一规范：参数抽取、相关性判断
# ============================================================
class IntentHandler(ABC):
    # handler 对应的意图模块名称
    name = ""
    
    @abstractmethod
    def extract_slots(self, query: str, is_followup: bool = False) -> dict:
        """
        从用户输入中提取当前模块需要的参数
        每个子类必须实现
        is_followup：追问轮置 True，只抽填空字段（人名/时间/ID 等），
        不重判子类型，避免短回复的默认值/误判污染原查询
        """
        pass
    
    @abstractmethod
    def is_related(self, state, query: str) -> bool:
        """
        判断用户回复是否与当前追问任务相关
        每个子类必须实现
        """
        pass


# ============================================================
# 确认等待态（_pending_confirm）的同意判定
# ============================================================
# 2026-09-23 冒烟 T4 发现：确认等待期用户改发新查询「查一下人员位置」，
# 被 AGREE_KEYWORDS 的单字「查」子串命中，误当成同意、旧查询被照常执行。
# 因此确认态的同意判定收紧为「整句白名单」：去掉标点/空白与结尾语气词
# 后必须与确认语完全相等才放行；带业务词的长句一律落到 re_ask（追问态
# 不跑意图分类，跨域新查询随后由 app.py 情况 1.2 的 give_up/接管窗口处理）。
_CONFIRM_AGREE_PHRASES = {
    "好", "好的", "嗯", "嗯嗯", "可以", "行", "要", "要得", "中",
    "对", "是", "是的", "没错", "没问题", "需要", "ok", "yes",
    "查", "查一下", "查下", "看", "看一下", "看下", "看看", "展示", "展示吧",
}
# 归一化：去空白与中英文常用标点
_CONFIRM_AGREE_STRIP_RE = re.compile(r"[\s，,。.!！?？、;；：:~～\"'“”‘’]")
# 结尾语气词（rstrip 集合，如「行吧」「是的呢」归一后命中白名单）
_CONFIRM_AGREE_TAIL = "吧呢哦哈呀啦了的嘞"


def is_confirm_agree(query: str) -> bool:
    """确认等待态的同意判定：整句（去标点/结尾语气词后）是白名单确认语才算同意。

    不能用子串匹配——「查一下人员位置」含「查」会被误吞成同意（冒烟 T4）。
    """
    q = _CONFIRM_AGREE_STRIP_RE.sub("", str(query or "")).lower()
    q = q.rstrip(_CONFIRM_AGREE_TAIL)
    return q in _CONFIRM_AGREE_PHRASES