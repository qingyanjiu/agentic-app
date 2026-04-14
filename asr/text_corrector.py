# text_corrector.py
"""
文本纠错模块
支持多种纠错方式：规则匹配、pycorrector、自定义模型
"""

import re
import logging
from typing import Tuple, List, Dict, Optional, Union

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 尝试导入pycorrector
try:
    from pycorrector import correct
    PYCORRECTOR_AVAILABLE = True
    logger.info("pycorrector已加载")
    
    # 尝试加载MacBERT模型
    try:
        from pycorrector.macbert.macbert_corrector import MacBertCorrector
        MACBERT_AVAILABLE = True
        logger.info("MacBERT模型可用")
    except ImportError:
        MACBERT_AVAILABLE = False
        logger.info("MacBERT模型不可用，使用基础pycorrector")
except ImportError:
    PYCORRECTOR_AVAILABLE = False
    MACBERT_AVAILABLE = False
    logger.warning("pycorrector未安装，将使用规则纠错")

# 简单规则纠错映射表
SIMPLY_CORRECT_MAP = {
    # 常见同音字错误
    "公调": "空调", "晴郎": "晴朗", "公圆": "公园", "知到": "知道",
    "因该": "应该", "坐位": "座位", "麻省": "马上", "在见": "再见",
    "时后": "时候", "以经": "已经", "那吗": "那么", "在在": "在",
    "了了": "了", "的的": "的", "地地": "地", "得得": "得",
    "己经": "已经", "未尾": "末尾", "折学": "哲学", "优其": "尤其",
    "其怪": "奇怪", "坚苦": "艰苦", "克苦": "刻苦", "刻服": "克服",
    # 常见多字/少字错误
    "因该该": "因该", "可以以": "可以", "是不是是": "是不是",
    "不知道道": "不知道", "为什么么": "为什么",
}

# 专业术语保护列表
PROTECTED_TERMS = [
    "深度学习", "神经网络", "人工智能", "机器学习", "自然语言处理",
    "计算机视觉", "数据挖掘", "Python", "Java", "C\\+\\+", "JavaScript",
    "API", "SDK", "HTTP", "HTTPS", "WebSocket", "FastAPI", "Whisper",
]

class TextCorrector:
    """文本纠错器"""
    
    def __init__(self, use_advanced: bool = True, use_protected_terms: bool = True):
        """
        初始化纠错器
        
        Args:
            use_advanced: 是否使用高级纠错（pycorrector）
            use_protected_terms: 是否使用专业术语保护
        """
        self.use_advanced = use_advanced and PYCORRECTOR_AVAILABLE
        self.use_protected_terms = use_protected_terms
        
        # 高级纠错模型（懒加载）
        self._macbert_corrector = None
        
        # 编译正则表达式用于保护术语
        if self.use_protected_terms:
            self.protected_pattern = re.compile(
                '|'.join(re.escape(term) for term in PROTECTED_TERMS),
                re.IGNORECASE
            )
        
        # 统计信息
        self.stats = {
            "total_corrections": 0,
            "rule_based": 0,
            "advanced": 0
        }
        
        logger.info(f"TextCorrector初始化 - 高级纠错: {self.use_advanced}, 术语保护: {use_protected_terms}")
    
    @property
    def macbert_corrector(self):
        """懒加载MacBERT纠错模型"""
        if self._macbert_corrector is None and self.use_advanced and MACBERT_AVAILABLE:
            try:
                logger.info("正在加载MacBERT纠错模型...")
                self._macbert_corrector = MacBertCorrector()
                logger.info("MacBERT纠错模型加载完成")
            except Exception as e:
                logger.error(f"加载MacBERT模型失败: {e}")
        return self._macbert_corrector
    
    def _protect_terms(self, text: str) -> Tuple[str, Dict[str, str]]:
        """保护专业术语"""
        if not self.use_protected_terms:
            return text, {}
        
        placeholders = {}
        protected_text = text
        
        matches = self.protected_pattern.finditer(text)
        for i, match in enumerate(matches):
            term = match.group()
            placeholder = f"__PROTECTED_{i}__"
            protected_text = protected_text.replace(term, placeholder)
            placeholders[placeholder] = term
        
        return protected_text, placeholders
    
    def _restore_terms(self, text: str, placeholders: Dict[str, str]) -> str:
        """恢复专业术语"""
        restored_text = text
        for placeholder, term in placeholders.items():
            restored_text = restored_text.replace(placeholder, term)
        return restored_text
    
    def rule_based_correct(self, text: str) -> Tuple[str, List[Tuple[str, str]]]:
        """基于规则的文本纠错"""
        if not text or not text.strip():
            return text, []
        
        corrected = text
        errors = []
        
        # 按长度降序排序
        sorted_map = sorted(SIMPLY_CORRECT_MAP.items(), key=lambda x: len(x[0]), reverse=True)
        
        for wrong, right in sorted_map:
            if wrong in corrected:
                corrected = corrected.replace(wrong, right)
                errors.append((wrong, right))
                self.stats["rule_based"] += 1
        
        return corrected, errors
    
    def advanced_correct(self, text: str) -> Tuple[str, List]:
        """高级文本纠错"""
        if not self.use_advanced or not text:
            return self.rule_based_correct(text)
        
        try:
            corrected, details = correct(text)
            self.stats["advanced"] += 1
            
            # 二次规则纠错
            if len(corrected) > 0:
                corrected, extra_errors = self.rule_based_correct(corrected)
                if extra_errors:
                    details.extend(extra_errors)
                return corrected, details
            else:
                return self.rule_based_correct(text)
                
        except Exception as e:
            logger.error(f"高级纠错失败: {e}")
            return self.rule_based_correct(text)
    
    def macbert_correct(self, text: str) -> Tuple[str, List]:
        """使用MacBERT模型纠错"""
        if not self.use_advanced or not text:
            return self.rule_based_correct(text)
        
        try:
            if self.macbert_corrector:
                corrected = self.macbert_corrector.macbert_correct(text)
                self.stats["advanced"] += 1
                # 二次规则纠错
                corrected, extra_errors = self.rule_based_correct(corrected)
                return corrected, extra_errors
            else:
                return self.advanced_correct(text)
        except Exception as e:
            logger.error(f"MacBERT纠错失败: {e}")
            return self.rule_based_correct(text)
    
    def correct(self, text: str, method: str = "auto") -> Tuple[str, List]:
        """
        统一的纠错接口
        
        Args:
            text: 输入文本
            method: 纠错方法 - "auto", "rule", "advanced", "macbert"
        """
        if not text or not text.strip():
            return text, []
        
        # 保护专业术语
        protected_text, placeholders = self._protect_terms(text)
        
        # 根据方法选择纠错策略
        if method == "rule":
            corrected, errors = self.rule_based_correct(protected_text)
        elif method == "advanced":
            corrected, errors = self.advanced_correct(protected_text)
        elif method == "macbert":
            corrected, errors = self.macbert_correct(protected_text)
        else:  # auto
            # 短文本使用高级纠错，长文本使用规则纠错
            if len(protected_text) < 50 and self.use_advanced:
                corrected, errors = self.advanced_correct(protected_text)
            else:
                corrected, errors = self.rule_based_correct(protected_text)
        
        # 恢复专业术语
        corrected = self._restore_terms(corrected, placeholders)
        
        self.stats["total_corrections"] += len(errors)
        
        return corrected, errors
    
    def batch_correct(self, texts: List[str], method: str = "auto") -> List[Tuple[str, List]]:
        """批量纠错"""
        results = []
        for text in texts:
            results.append(self.correct(text, method))
        return results
    
    def correct_with_confidence(self, text: str) -> Dict:
        """带置信度的纠错"""
        corrected, errors = self.correct(text)
        
        # 计算置信度
        if len(text) > 0:
            confidence = 1.0 - (len(errors) / len(text))
            confidence = max(0, min(1, confidence))
        else:
            confidence = 1.0
        
        return {
            "original": text,
            "corrected": corrected,
            "errors": errors,
            "confidence": confidence,
            "error_count": len(errors)
        }
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return self.stats.copy()

# 全局单例
_default_corrector = None

def get_corrector(use_advanced: bool = True) -> TextCorrector:
    """获取全局纠错器实例"""
    global _default_corrector
    if _default_corrector is None:
        _default_corrector = TextCorrector(use_advanced=use_advanced)
    return _default_corrector

# 便捷函数
def correct_text(text: str, method: str = "auto") -> Tuple[str, List]:
    """快速纠错"""
    corrector = get_corrector()
    return corrector.correct(text, method)

def quick_correct(text: str) -> str:
    """快速纠错，只返回文本"""
    corrected, _ = correct_text(text)
    return corrected

# 测试代码
if __name__ == "__main__":
    test_texts = [
        "今天天气晴郎",
        "我们去公圆玩",
        "少先队员因该让坐",
        "深度学习模型表现优异",  # 专业术语
    ]
    
    print("文本纠错测试")
    print("=" * 40)
    
    corrector = get_corrector()
    for text in test_texts:
        corrected, errors = corrector.correct(text)
        print(f"原始: {text}")
        print(f"纠错: {corrected}")
        if errors:
            print(f"修正: {errors}")
        print()