import os
import tempfile
import base64
import subprocess
import re
from pathlib import Path
import requests

class WhisperASR:
    def __init__(self):
        # 你的语音识别程序路径
        self.main_path = "/root/agentic-app/whisper.cpp/build/bin/whisper-cli"
        self.model_path = "/root/agentic-app/whisper.cpp/models/ggml-medium.bin"
        # 支持的音频格式
        self.supported_formats = (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".webm")

    def _check_ffmpeg(self):
        """检查 FFmpeg 是否可用（支持更多格式必须要它）"""
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
            print("✅ FFmpeg 正常可用 → 全格式音频支持")
            return True
        except:
            print("❌ FFmpeg 未安装 → 仅支持 WAV 格式")
            return False

    def _is_base64(self, data):
        """判断是不是 base64 字符串"""
        if not isinstance(data, str):
            return False
         # 只要包含 base64 头部 或 是纯 base64 字符串，都算
        if data.startswith("data:audio/"):
            return True
        
        # 纯 base64 字符串判断（更宽松、更准）
        try:
            # 能正常解码就是 base64
            base64.b64decode(data, validate=True)
            return True
        except:
            return False
       

    def _is_url(self, data):
        """判断是不是网络 URL"""
        return isinstance(data, str) and (data.startswith("http://") or data.startswith("https://"))

    def _is_file(self, data):
        """判断是不是本地文件路径"""
        try:
            return isinstance(data, str) and Path(data).exists()
        except:
            return False

    def _save_to_tempfile(self, audio_data, suffix=".wav"):
        """保存二进制到临时文件"""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_data)
            return f.name

    def transcribe(self, audio_input):
        """
        🔥 万能识别接口！支持 4 种输入：
        1. Base64 字符串（data:audio/wav;base64,xxx）
        2. 本地文件路径（/xxx/test.mp3）
        3. 网络 URL（http://xxx/audio.mp3）
        4. 二进制音频 bytes
        """
        ffmpeg_exists = self._check_ffmpeg()

        try:
            tmp_path = None

            # ==========================
            # 1. 输入是 Base64
            # ==========================
            if self._is_base64(audio_input):
                print("🔍 输入类型：Base64")
                # 兼容带头部的 base64
                if "base64," in audio_input:
                    audio_input = audio_input.split("base64,")[1]
                audio_bytes = base64.b64decode(audio_input)
                tmp_path = self._save_to_tempfile(audio_bytes)

            # ==========================
            # 2. 输入是本地文件
            # ==========================
            elif self._is_file(audio_input):
                print("🔍 输入类型：本地文件")
                tmp_path = audio_input

            # ==========================
            # 3. 输入是网络 URL
            # ==========================
            elif self._is_url(audio_input):
                print("🔍 输入类型：网络URL")
                resp = requests.get(audio_input, timeout=10)
                tmp_path = self._save_to_tempfile(resp.content)

            # ==========================
            # 4. 输入是二进制 bytes
            # ==========================
            elif isinstance(audio_input, bytes):
                print("🔍 输入类型：二进制音频")
                tmp_path = self._save_to_tempfile(audio_input)

            else:
                return "❌ 不支持的输入格式"

            # ==========================
            # 开始语音识别
            # ==========================
            result = subprocess.run(
                [
                    self.main_path,
                    "-m", self.model_path,
                    "-f", tmp_path,
                    "-l", "zh",
                    "-nt",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            # 只删除我们创建的临时文件
            if tmp_path != audio_input and Path(tmp_path).exists():
                os.unlink(tmp_path)

            text = result.stdout.strip()
            return text if text else "❌ 未识别到语音"

        except Exception as e:
            print("ASR 异常：", e)
            return "❌ 识别失败"

# 创建全局实例
asr = WhisperASR()