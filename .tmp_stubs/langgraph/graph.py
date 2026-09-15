# 临时 stub（本机验证用，验证后删除）：容器内使用真实包
END = "__end__"


class _Nodes:
    def __getitem__(self, k):
        return None


class StateGraph:
    def __init__(self, schema=None):
        self.schema = schema
        self.nodes = {}

    def add_node(self, *args, **kwargs):
        return self

    def add_edge(self, *args, **kwargs):
        return self

    def add_conditional_edges(self, *args, **kwargs):
        return self

    def set_entry_point(self, *args, **kwargs):
        return self

    def compile(self):
        return None