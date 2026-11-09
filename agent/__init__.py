"""项目内 agent 包。

这个 __init__.py 不是装饰：本机 site-packages 里存在另一个同名的 `agent`
（ensurance-agent 的 editable 安装），Python 会优先选择"常规包"而不是
无 __init__.py 的 namespace 包，导致 `import agent.loop` 跑到别的项目去。
显式声明为常规包后，工作目录下的 agent 始终优先。
"""
