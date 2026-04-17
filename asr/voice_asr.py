# asr/voice_asr.py
import base64
import numpy as np
import logging
from typing import Optional, Union, Dict
import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import os
import subprocess
import wave
import noisereduce as nr
os.environ["WHISPER_NO_PROGRESS_BAR"] = "1"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# 添加 opencc 导入
try:
    from opencc import OpenCC
    OPENCC_AVAILABLE = True
    # 创建简繁转换器（繁→简）
    cc = OpenCC('t2s')  # 繁体到简体
except ImportError:
    OPENCC_AVAILABLE = False
    logger.warning("opencc-python-reimplemented not installed,繁体转简体功能不可用")



try:
    import whisper
    whisper.utils.get_progress_bar = lambda *args, **kwargs: None
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False
    logger.warning("Whisper not installed")


# ========== 方案1：模块级单例模型缓存 ==========
_WHISPER_MODEL_CACHE = {}  # {model_name: model_instance}

def _get_cached_whisper_model(model_name: str):
    """获取缓存的 Whisper 模型（模块级单例）"""
    if model_name not in _WHISPER_MODEL_CACHE:
        logger.info(f"🔄 首次加载模型: {model_name}（这可能需要一些时间）")
        _WHISPER_MODEL_CACHE[model_name] = whisper.load_model(model_name)
        logger.info(f"✅ 模型 {model_name} 加载完成并已缓存")
    else:
        logger.debug(f"♻️ 复用已缓存的模型: {model_name}")
    
    return _WHISPER_MODEL_CACHE[model_name]



class AudioProcessor:
    @staticmethod
    def base64_to_bytes(base64_str: str) -> bytes:
        '''处理 Data URL 格式：data:audio/webm;base64,xxxxx 提取逗号后的纯 base64'''
        if ',' in base64_str:
            base64_str = base64_str.split(',')[1]
        return base64.b64decode(base64_str)

    @staticmethod
    def webm_to_pcm(webm_bytes: bytes):
        #检查输入有效性，过短的音频直接返回空
        if not webm_bytes or len(webm_bytes) < 100:
            return b"", 0, 0, 0
        # 明确指定参数
        sample_rate = 16000
        channels = 1
        sample_width = 2  # s16le = 16bit = 2字节
        #ffmpeg 命令行参数，从管道读取 WebM，输出 PCM 到管道
        cmd = [
            'ffmpeg',
            '-hide_banner',
            '-loglevel', 'error',
            '-i', 'pipe:0',
            '-f', 's16le',
            '-ar', str(sample_rate),  # 采样率
            '-ac', str(channels),      # 声道数
            '-c:a', 'pcm_s16le',
            'pipe:1'
        ]
        res = subprocess.run(
            cmd,
            input=webm_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30
        )
        # 添加失败检测
        if len(res.stdout) < 100:
            logger.warning(f"⚠️ WebM转PCM结果异常: {len(res.stdout)}字节")
            return b"", 0, 0, 0
        return res.stdout, sample_rate, channels, sample_width

    @staticmethod
    def pcm_to_numpy(pcm_data: bytes, sample_width: int = 2) -> np.ndarray:
        """转换PCM到numpy数组，支持不同的位深度"""
        if sample_width == 2:
            dtype = np.int16
        elif sample_width == 1:
            dtype = np.int8
        elif sample_width == 4:
            dtype = np.int32
        else:
            dtype = np.int16  # 默认16bit
        
        return np.frombuffer(pcm_data, dtype=dtype).astype(np.float32) / 32768.0


class WhisperASR:
    def __init__(self, model_name="medium", language="zh", convert_to_simplified=True):
        self.model_name = model_name
        self.language = language
        self.model = None
        self.initialized = False
        self.convert_to_simplified = convert_to_simplified  # 繁改简

        # 初始化转换器
        self.converter = None
        if self.convert_to_simplified and OPENCC_AVAILABLE:
            self.converter = OpenCC('t2s')
            logger.info("✅ 简繁转换器已初始化（繁→简）")
        elif self.convert_to_simplified and not OPENCC_AVAILABLE:
            logger.warning("⚠️ opencc未安装，无法进行繁转简")
            self.convert_to_simplified = False

        if not WHISPER_AVAILABLE:
            return

        try:
            # 使用缓存的模型（方案1）
            self.model = _get_cached_whisper_model(model_name)
            self.initialized = True
            logger.info(f"✅ Whisper 模型已就绪: {model_name}")
        except Exception as e:
            logger.error(f"模型加载失败: {e}")
    def _convert_text(self, text: str) -> str:
        """转换文本（繁→简）"""
        if not self.convert_to_simplified or not text:
            return text
        
        if self.converter:
            try:
                return self.converter.convert(text)
            except Exception as e:
                logger.debug(f"简繁转换失败: {e}")
                return text
        return text
    def transcribe(self, audio_bytes: bytes) -> str:
        if not self.initialized:
            logger.warning("Whisper未初始化")
            return ""
        
        if len(audio_bytes) < 1000:
            return ""

        try:
            pcm_data, sample_rate, channels, sample_width = AudioProcessor.webm_to_pcm(audio_bytes)
            if len(pcm_data) < 1600:
                return ""

            # 传入 sample_width 参数
            audio_np = AudioProcessor.pcm_to_numpy(pcm_data, sample_width)
            
            # 降噪处理
            try:
                audio_np = nr.reduce_noise(
                    y=audio_np, 
                    sr=sample_rate, 
                    stationary=True, 
                    prop_decrease=0.3  # 降低降噪强度
                )
            except Exception as e:
                logger.debug(f"降噪跳过: {e}")
            
            result = self.model.transcribe(
                audio_np,
                language=self.language,
                fp16=False,
                verbose=False,
                temperature=0.0
            )
            text = result["text"].strip()
            # 新增这一行：转换为简体
            text = self._convert_text(text)
            return text

        except Exception as e:
            logger.error(f"识别错误: {e}")
            return ""


class VoiceRecognizer:
    # ========== 方案3：类级别共享 ASR 引擎 ==========
    _shared_asr_engine = None
    _shared_engine_params = None
    
    def __init__(self, backend="whisper", model_name="base", language="zh", convert_to_simplified=True):
        self.backend = backend
        self.language = language
        self.buffers: Dict[str, bytes] = {}       # 实时识别用
        self.full_webm: Dict[str, bytes] = {}     # 保存完整原始WebM
        self.pcm_buffers: Dict[str, bytes] = {}   # 转好的PCM
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.convert_to_simplified = convert_to_simplified 

        # 复用 ASR 引擎（方案3）
        if backend == "whisper":
            # 检查是否需要创建新引擎
            current_params = (model_name, language, convert_to_simplified)
            
            if (VoiceRecognizer._shared_asr_engine is None or 
                VoiceRecognizer._shared_engine_params != current_params):
                
                logger.info(f"🔄 创建新的 ASR 引擎实例（model={model_name}, lang={language}）")
                VoiceRecognizer._shared_asr_engine = WhisperASR(
                    model_name=model_name, 
                    language=language,
                    convert_to_simplified=convert_to_simplified
                )
                VoiceRecognizer._shared_engine_params = current_params
            else:
                logger.debug(f"♻️ 复用已有的 ASR 引擎实例")
            
            self.asr_engine = VoiceRecognizer._shared_asr_engine
        else:
            self.asr_engine = None

        self.is_available = self.asr_engine.initialized if self.asr_engine else False
        logger.info(f"语音识别就绪: {self.is_available} (backend={backend}, model={model_name})")

    def get_buffer(self, session_id):
        if session_id not in self.buffers:
            self.buffers[session_id] = b""
        return self.buffers[session_id]

    def set_buffer(self, session_id, data):
        self.buffers[session_id] = data

    # 流式识别：不清空完整录音
    def transcribe_stream(self, session_id: str, chunk: Union[bytes, str]) -> str:
        try:
            # 1) 解码本次发来的音频片段
            if isinstance(chunk, str):
                audio_bytes = AudioProcessor.base64_to_bytes(chunk)
            else:
                audio_bytes = chunk

            # # 2) 永远追加到完整录音（用于最后保存，不动它）
            # if session_id not in self.full_webm:
            #     self.full_webm[session_id] = b""
            # self.full_webm[session_id] += audio_bytes

            # # 3) 识别用的临时缓冲区（累积够了才识别一次）
            # buf = self.get_buffer(session_id)
            # buf += audio_bytes
            # self.set_buffer(session_id, buf)

            if len(audio_bytes) < 10000:
                return ""
        
            return self.asr_engine.transcribe(audio_bytes)

            

        except Exception as e:
            logger.error(f"流识别错误: {e}")
            return ""
    
    # 保存完整录音
    def save_recording(self, session_id: str, save_dir: str = "./recordings") -> str:
        try:
            os.makedirs(save_dir, exist_ok=True)
            webm_data = self.full_webm.get(session_id, b"")

            if len(webm_data) < 500:
                logger.warning(f"⚠️ 录音太短（{len(webm_data)}字节），session_id={session_id}")
                return ""

            # ✅ 修复：使用正确的变量名 webm_data
            pcm_data, sample_rate, channels, sample_width = AudioProcessor.webm_to_pcm(webm_data)
            if len(pcm_data) == 0:
                logger.error("❌ WebM转PCM失败，无音频")
                return ""

            # 写标准WAV
            wav_path = os.path.join(save_dir, f"recording_{session_id}.wav")
            with wave.open(wav_path, "wb") as w:
                w.setnchannels(channels)
                w.setsampwidth(sample_width)
                w.setframerate(sample_rate)
                w.writeframes(pcm_data)

            logger.info(f"✅ 录音已保存: {wav_path} ({len(pcm_data)}字节)")
            return wav_path

        except Exception as e:
            logger.error(f"❌ 保存录音失败: {e}")
            return ""

    async def transcribe_stream_async(self, session_id: str, chunk: Union[bytes, str]) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self.executor, self.transcribe_stream, session_id, chunk)

    def get_status(self):
        return {
            "available": self.is_available, 
            "backend": self.backend, 
            "language": self.language,
            "model_name": getattr(self.asr_engine, 'model_name', None) if self.asr_engine else None
        }

    def is_sentence_end(self, text):
        return text.endswith(("。", "？", "！", ".", "?", "!"))

    def remove_session(self, session_id):
        self.buffers.pop(session_id, None)
        self.full_webm.pop(session_id, None)
        self.pcm_buffers.pop(session_id, None)
    
    @classmethod
    def clear_model_cache(cls):
        """清理模型缓存（可选，用于释放内存）"""
        cls._shared_asr_engine = None
        cls._shared_engine_params = None
        global _WHISPER_MODEL_CACHE
        _WHISPER_MODEL_CACHE.clear()
        logger.info("🧹 模型缓存已清理")


# 全局默认识别器实例
_default_recognizer = None

def get_recognizer(backend="whisper", model_name="base", use_stream=True, convert_to_simplified=True):
    """获取全局识别器实例（单例）"""
    global _default_recognizer
    
    if _default_recognizer is None:
        logger.info("🚀 创建默认识别器实例")
        _default_recognizer = VoiceRecognizer(backend=backend, model_name=model_name,
            convert_to_simplified=convert_to_simplified)
    else:
        # 可选：检查参数是否匹配，不匹配时警告
        if (_default_recognizer.backend != backend or 
            _default_recognizer.language != getattr(_default_recognizer, 'language', 'zh')):
            logger.warning(f"已有识别器使用不同参数，将复用现有实例（忽略新参数）")
    
    return _default_recognizer