# asr_pipeline.py
"""
ASR处理流水线模块
包含语音识别、文本纠错等功能
"""
from agent.text_corrector_prompt import gen_prompt
from langchain_core.language_models import BaseChatModel
from langgraph.types import StreamWriter
import json
import logging
from langchain_classic.chains.llm import LLMChain
import asyncio
import time
from typing import Optional
from fastapi import WebSocket, WebSocketDisconnect  
import uuid  
from langchain_core.messages import SystemMessage, HumanMessage  


logging.basicConfig(
    filename='app.log',
    # 追加模式 'a'，覆盖模式 'w' 
    filemode='w',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger(__name__) 

class TextCorrectorPipeline:
    """OpenAI API文本纠错器"""
    
    def __init__(
       self, 
       llm: BaseChatModel,
       user_id: str, 
       session_id: str, 
       max_iters: int = 3,
       enable_debug=False
    ):
        
        
        self.llm = llm
        self.user_id = user_id
        self.session_id = session_id
        self.max_iters = max_iters#最大迭代次数，防止无限循环（如工具调用失败时的重试）
        self.enable_debug = enable_debug#调试模式开关，控制日志详细程度
        self.chain: LLMChain | None = None

    async def _init_chain(self, prompt):
        """初始化 LLM 链（避免重复创建）"""
        if self.chain is None:
            self.chain = LLMChain(llm=self.llm, prompt=prompt)
        return self.chain
    # 模拟流式输出,将完整的文本分块、延时发送，模拟大模型生成内容时的流式输出效果。
    async def fake_stream(
        self,
        text: str,# 要模拟输出的完整文本
        writer: StreamWriter,# 写入器（回调函数），用于发送每个数据块
        step=2,# 每次发送的字符数（块大小）
        delay: float = 0.05,# 块之间的延迟时间（秒）
    ):
        for i in range(0, len(text), step): # 按步长遍历文本
            output = {"type": "answer", "content": text[i: i+step]} # 构造数据块
            writer(output) # 发送当前块
            await asyncio.sleep(delay)
         
    async def correct(self, text: str, writer: StreamWriter, **kwargs) -> str:
        """
        AI 文本纠错主函数
        :param text: ASR 原始文本
        :param writer: 流式输出回调
        :return: 纠错后文本
        """
        # 空文本直接返回
        if not text or len(text.strip()) == 0:
            return text
        
        
        
        try:
            prompt = await gen_prompt(text)
            # self.get_correct_agent=await self._init_chain(prompt)
            # result = self.get_correct_agent.invoke({"text": text})
            result = await self.llm.ainvoke([
                SystemMessage(content=prompt),  # 系统提示
                HumanMessage(content=text)] )     # 用户文本)
            result = result.content.strip() 
            # result = json.loads(result.get('text', '{}'))#result.get('text', {}) - 获取文本内容,json.loads(...) - JSON解析
            # if writer:
            #     await self.fake_stream(result, writer)
        # 3. 解析结果
            # corrected = response.choices[0].message.content.strip()
         
            return self._post_process(result, text)
            
        except asyncio.TimeoutError:
            logger.warning(f"AI纠错超时: {text}")
            return text
        except Exception as e:
            logger.error(f"AI纠错失败: {e}")
            return text
   
    def _post_process(self, corrected: str, original: str) -> str:
        """后处理：清理格式、冗余前缀、空值"""
        if not corrected:
            return original

        # 移除常见前缀
        prefix_list = [
            "纠正后的文本：",
            "修正后的文本：",
            "修改后的文本：",
            "纠错结果：",
            "答案："
        ]
        for prefix in prefix_list:
            if corrected.startswith(prefix):
                corrected = corrected.replace(prefix, "").strip()

        return corrected.strip()

