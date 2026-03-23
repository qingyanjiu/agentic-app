import requests
'''Dify 知识库系统交互的客户端封装类'''
from utils.utils import get_config

# ======== 定义知识库控制器 ========
class DifyKnowledgeBaseController:
    def __init__(self, base_url: str, dataset_id: str):
        self.base_url = base_url.rstrip("/") # Dify 服务的基础 URL,移除 base_url 末尾的斜杠，确保后续拼接 URL 时不会出现双斜杠
        self.dataset_id = dataset_id # 操作的知识库 ID
        self.config_file_path = 'dify-config.json' # 保存知识库 ID，后续所有请求都需要这个 ID。
        self.dify_config = get_config()['dify'] # 指定配置文件路径
        # 构建 HTTP 请求头，包含：
        # Authorization: Bearer 令牌认证（Dify API 的认证方式）
        # Content-Type: 指定请求体为 JSON 格式
        self.headers = {
            "Authorization": f"Bearer {self.dify_config['datasets_api_key']}",
            "Content-Type": "application/json"
        }
      #知识库检索  
    def search(self, query: str):
        """调用 Dify 检索接口"""
        url = f"{self.base_url}/v1/datasets/{self.dataset_id}/retrieve"
        resp = requests.post(url, headers=self.headers, json={"query": query})
        resp.raise_for_status()
        return resp.json().get("records", [])

    def list_documents(self, page: int = 1, page_size: int = 100):
        """列出知识库文件"""
        url = f"{self.base_url}/v1/datasets/{self.dataset_id}/documents?page={page}&limit={page_size}"
        resp = requests.get(url, headers=self.headers)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def list_datasets(self, page: int = 1, page_size: int = 100):
        """获取知识库列表信息"""
        url = f"{self.base_url}/v1/datasets?page={page}&limit={page_size}"
        resp = requests.get(url, headers=self.headers)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def get_document_segments(self, doc_id: str, page: int = 1, limit: int = 1):
        """读取文档的分段内容"""
        url = f"{self.base_url}/v1/datasets/{self.dataset_id}/documents/{doc_id}/segments?status=completed&page={page}&limit={limit}"
        resp = requests.get(url, headers=self.headers)
        resp.raise_for_status()
        return resp.json().get("data", [])