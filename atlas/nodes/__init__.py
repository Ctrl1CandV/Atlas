# -*- coding: utf-8 -*-
"""节点类型的封闭注册表(红线 ① 的代码侧)。

加新类型 = 在这里写工厂函数 + spec.py 的 NODE_TYPES,发版本。
YAML 里只能引用清单里的类型,不能注入代码。
"""
from atlas.nodes.agent import make_agent_node_fn

__all__ = ["make_agent_node_fn"]
