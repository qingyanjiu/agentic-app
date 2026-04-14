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

os.environ["WHISPER_NO_PROGRESS_BAR"] = "1"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    import whisper
    whisper.utils.get_progress_bar = lambda *args, **kwargs: None
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False
    logger.warning("Whisper not installed")


class AudioConfig:
    SAMPLE_RATE = 16000
    CHANNELS = 1
    SAMPLE_WIDTH = 2
    CHUNK_DURATION = 1.0
    CHUNK_SIZE = int(SAMPLE_RATE * SAMPLE_WIDTH * CHUNK_DURATION)


class AudioProcessor:
    @staticmethod
    def base64_to_bytes(base64_str: str) -> bytes:
        if ',' in base64_str:
            base64_str = base64_str.split(',')[1]
        return base64.b64decode(base64_str)

    @staticmethod
    def webm_to_pcm(webm_bytes: bytes) -> bytes:
        if not webm_bytes or len(webm_bytes) < 100:
            return b""
        cmd = [
            'ffmpeg',
            '-hide_banner',
            '-loglevel', 'error',
            '-i', 'pipe:0',
            '-f', 's16le',
            '-ar', '16000',
            '-ac', '1',       # ✅ 字符串，不是数字
            '-c:a', 'pcm_s16le',
            'pipe:1'
        ]
        res = subprocess.run(
            cmd,
            input=webm_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        return res.stdout

    @staticmethod
    def pcm_to_numpy(pcm_data: bytes) -> np.ndarray:
        return np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0


class WhisperASR:
    def __init__(self, model_name="base", language="zh"):
        self.model_name = model_name
        self.language = language
        self.model = None
        self.initialized = False

        if not WHISPER_AVAILABLE:
            return

        try:
            self.model = whisper.load_model(model_name)
            self.initialized = True
            logger.info("✅ Whisper 模型加载完成")
        except Exception as e:
            logger.error(f"模型加载失败: {e}")

    def transcribe(self, audio_bytes: bytes) -> str:
        if not self.initialized or len(audio_bytes) < 1000:
            return ""

        try:
            pcm_data = AudioProcessor.webm_to_pcm(audio_bytes)
            if len(pcm_data) < 1600:
                return ""

            audio_np = AudioProcessor.pcm_to_numpy(pcm_data)
            result = self.model.transcribe(
                audio_np,
                language=self.language,
                fp16=False,
                verbose=False,
                temperature=0.0
            )
            return result["text"].strip()

        except Exception as e:
            logger.error(f"识别错误: {e}")
            return ""


class VoiceRecognizer:
    def __init__(self, backend="whisper", model_name="base", language="zh"):
        self.backend = backend
        self.language = language
        self.buffers: Dict[str, bytes] = {}       # 实时识别用（可清空）
        self.full_webm: Dict[str, bytes] = {}      # ✅ 保存完整原始WebM（全程不清空）
        self.pcm_buffers: Dict[str, bytes] = {}    # 转好的PCM（可选）
        self.executor = ThreadPoolExecutor(max_workers=4)

        if backend == "whisper":
            self.asr_engine = WhisperASR(model_name=model_name, language=language)
        else:
            self.asr_engine = None

        self.is_available = self.asr_engine.initialized if self.asr_engine else False
        logger.info(f"语音识别就绪: {self.is_available}")

    def get_buffer(self, session_id):
        if session_id not in self.buffers:
            self.buffers[session_id] = b""
        return self.buffers[session_id]

    def set_buffer(self, session_id, data):
        self.buffers[session_id] = data

    # ------------------------------
    # ✅ 流式识别：不清空完整录音
    # ------------------------------
    def transcribe_stream(self, session_id: str, chunk: Union[bytes, str]) -> str:
        try:
            # 1) 解码本次发来的音频片段
            if isinstance(chunk, str):
                audio_bytes = AudioProcessor.base64_to_bytes(chunk)
            else:
                audio_bytes = chunk

            # 2) 永远追加到完整录音（用于最后保存，不动它）
            if session_id not in self.full_webm:
                self.full_webm[session_id] = b""
            self.full_webm[session_id] += audio_bytes

            # 3) 识别用的临时缓冲区（累积够了才识别一次）
            buf = self.get_buffer(session_id)
            buf += audio_bytes
            self.set_buffer(session_id, buf)

            # 4) 只有累积到足够长度，才识别一次
            # 够长 → 识别 → 清空缓冲区 → 不重复
            if len(buf) >= 12000:
                # 用这一段有效语音去识别，准确率高
                text = self.asr_engine.transcribe(buf)
                # 识别完清空，下次只识别新说的内容
                self.buffers[session_id] = b""
                return text

            return ""

        except Exception as e:
            logger.error(f"流识别错误: {e}")
            return ""
    # ------------------------------
    # ✅ 保存完整录音（从full_webm转）
    # ------------------------------
    def save_recording(self, session_id: str, save_dir: str = "./recordings") -> str:
        try:
            os.makedirs(save_dir, exist_ok=True)
            webm_data = self.full_webm.get(session_id, b"")

            if len(webm_data) < 500:
                logger.warning(f"⚠️ 录音太短（{len(webm_data)}字节），session_id={session_id}")
                return ""

            # 1) 完整WebM → PCM
            pcm_data = AudioProcessor.webm_to_pcm(webm_data)
            if len(pcm_data) == 0:
                logger.error("❌ WebM转PCM失败，无音频")
                return ""

            # 2) 写标准WAV
            wav_path = os.path.join(save_dir, f"recording_{session_id}.wav")
            with wave.open(wav_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
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
        return {"available": self.is_available, "backend": self.backend, "language": self.language}

    def is_sentence_end(self, text):
        return text.endswith(("。", "？", "！", ".", "?", "!"))

    def remove_session(self, session_id):
        self.buffers.pop(session_id, None)
        self.full_webm.pop(session_id, None)   # ✅ 清理完整录音
        self.pcm_buffers.pop(session_id, None)


_default_recognizer = None

def get_recognizer(backend="whisper", model_name="base", use_stream=True):
    global _default_recognizer
    if _default_recognizer is None:
        _default_recognizer = VoiceRecognizer(backend=backend, model_name=model_name)
    return _default_recognizer