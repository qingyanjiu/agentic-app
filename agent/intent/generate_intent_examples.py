"""
意图示例语料生成 / 扩充脚本
==============================
用法：
    python agent/intent/generate_intent_examples.py
        # 全量：生成 intent_seeds.py 里全部意图的示例
    python agent/intent/generate_intent_examples.py <字典名> [<字典名> ...]
        # 只生成指定意图，字典名为 ALL_INTENT_SEEDS 里的 key，如：
        # python agent/intent/generate_intent_examples.py EMERGENCY_SECURITY_STATUS_EXAMPLES COMPOSITIVE_OVERVIEW_STATUS_EXAMPLES

功能：
    1. 无种子生成：根据下面 INTENT_SEEDS 里定义的意图和子类型，从零生成示例。
    2. 有种子扩写：读取 agent/intent/classifier.py 中已有的 EXAMPLE 字典，进行扩写。

模式切换：
    SEEDLESS_MODE = True   # 无种子生成（默认）
    SEEDLESS_MODE = False  # 基于已有示例扩写

输出：
    - 自动更新 agent/intent/classifier.py 中的示例字典
      （intent_seeds 与 classifier 字典命名不一致的，按脚本内 CLASSIFIER_DICT_ALIASES 映射定位；
        只覆盖两边都有的子类型，classifier 独有的子类型原样保留）
    - 自动备份原文件为 classifier.py.bak
    - 同时生成 JSON 检查文件到 agent/intent/generated_examples/

说明：
    - 脚本调用 DeepSeek API，请确保 OPENAI_API_KEY 或 SILICON_API_KEY 已设置。
    - 默认每个子类型生成 25 条示例，可在 SUB_TYPE_TARGET_COUNT 调整。
    - 为增加口气多样性，脚本按 4 种风格分别生成：疑问句、省略主语的疑问句、口语化疑问、场景化疑问。
    - 所有风格统一硬性要求：必须是疑问句（带疑问词）、4~20 字、口语化，像真实用户会问出的问题。
"""

import os
import re
import sys
import json
import ast
import shutil
from typing import Dict, List
from datetime import datetime

try:
    from openai import OpenAI
    _NEW_OPENAI = True
except ImportError:
    import openai
    _NEW_OPENAI = False

# ============================================================
# DeepSeek API 配置（与项目 models/llm.py 保持一致）
# ============================================================
_MODEL_URL = "https://api.deepseek.com"
API_KEY = os.getenv("SILICON_API_KEY") if os.getenv("SILICON_API_KEY") else os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = "deepseek-v4-flash"
BASE_URL = _MODEL_URL if _MODEL_URL.endswith("/v1") else _MODEL_URL.rstrip("/") + "/v1"

if not API_KEY or "xxxx" in API_KEY or len(API_KEY) < 10:
    raise ValueError(
        "请在环境变量中设置真实的 API Key：\n"
        "  优先读取 SILICON_API_KEY\n"
        "  否则读取 OPENAI_API_KEY\n"
        "例如：set OPENAI_API_KEY=sk-xxx"
    )

if _NEW_OPENAI:
    client = OpenAI(base_url=BASE_URL, api_key=API_KEY)
else:
    openai.api_base = BASE_URL
    openai.api_key = API_KEY
    client = None


# ============================================================
# 模式开关
# ============================================================
# True  = 无种子生成：完全根据 INTENT_SEEDS 生成新示例，覆盖 classifier.py 中同名字典
# False = 扩写模式：读取 classifier.py 已有示例并扩写
SEEDLESS_MODE = True

# 是否把新示例和 classifier.py 里已有的同名子类型示例合并（仅 SEEDLESS_MODE=True 时生效）
# True  = 合并去重；False = 完全覆盖
MERGE_EXISTING = True


# ============================================================
# 目标数量与生成风格
# ============================================================
SUB_TYPE_TARGET_COUNT = 25

STYLES = [
    {
        "name": "一般疑问",
        "desc": "常规疑问句提问，口语化、简短，带'吗/呢/多少/有没有/在哪/怎么/哪些'等疑问词，比如'XX现在有多少'、'XX在哪能看'",
    },
    {
        "name": "省略主语疑问",
        "desc": "省略主语的疑问句，比如'查下XX吗'、'看下XX有多少'、'给我看看XX咋样'，不要主语，但必须带疑问语气，禁止纯祈使",
    },
    {
        "name": "口语化疑问",
        "desc": "非常随意的口语化疑问，可带省略、方言感，比如'还有么'、'有没有XX'、'XX啥情况'风格的问句",
    },
    {
        "name": "场景化疑问",
        "desc": "带轻微场景或限制的疑问，比如'急用，XX在哪'、'店里有没有XX'、'我现在就要看，XX多少'",
    },
]


# ============================================================
# 无种子生成配置：从 intent_seeds.py 加载
# 使用 importlib 直接加载，避免触发 agent.intent 包的 __init__
# （__init__ 会导入 classifier.py，而 classifier.py 需要 sentence_transformers）
# 如需新增/修改意图，请编辑 agent/intent/intent_seeds.py
# ============================================================
def _load_intent_seeds() -> Dict[str, Dict[str, str]]:
    import importlib.util
    seeds_path = os.path.join(os.path.dirname(__file__), "intent_seeds.py")
    spec = importlib.util.spec_from_file_location("intent_seeds", seeds_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ALL_INTENT_SEEDS


INTENT_SEEDS: Dict[str, Dict[str, str]] = _load_intent_seeds()


# ============================================================
# Prompt 构造
# ============================================================
def build_prompt(
    intent_name: str,
    intent_desc: str,
    sub_type: str,
    sub_type_desc: str,
    style_name: str,
    style_desc: str,
    existing_examples: List[str],
    count: int = 8,
) -> str:
    """为单个子类型、单种风格构造生成 Prompt"""
    if existing_examples:
        examples_text = "\n".join([f"  {i + 1}. {ex}" for i, ex in enumerate(existing_examples[:5])])
        few_shot_section = f"参考表达（不要复制，仅作风格参考）：\n{examples_text}\n\n"
    else:
        few_shot_section = ""

    prompt = f"""你是一位真实的智慧园区用户，正在线上平台用自然语言提问。

意图大类：{intent_name}
意图说明：{intent_desc}
子类型：{sub_type}（{sub_type_desc}）

任务：
请生成 {count} 条属于“{sub_type}”的用户原话，风格要求：{style_desc}。

约束：
- 每条必须是疑问句，句中带疑问词（吗/呢/多少/有没有/在哪/怎么/哪些/什么等），禁止纯陈述句或祈使句。
- 每条 4~20 个字，像真实用户在输入框里敲出来的短问句。
- 必须口语化，是真实用户在“{sub_type}（{sub_type_desc}）”场景下自然会问出的问题，不要书面语、不要机器味、避免礼貌用语（不要“请问”“您好”“谢谢”）。
- 疑问口气要符合“{style_name}”风格，但不论哪种风格都必须是疑问句。
- 每条表达必须和子类型含义强相关，不要跑题。
- 不要重复或近似表达，必须覆盖不同角度。
- 不要解释，直接输出 {count} 条，每行一条，前面加序号。

{few_shot_section}输出格式：
1. ...
2. ...
...
"""
    return prompt


# ============================================================
# LLM 调用与解析
# ============================================================
def call_llm(prompt: str, temperature: float = 0.85) -> str:
    """调用 DeepSeek 并返回文本，兼容新旧 openai 库"""
    try:
        if _NEW_OPENAI:
            completion = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
            return completion.choices[0].message.content or ""
        else:
            completion = openai.ChatCompletion.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
            )
            return completion["choices"][0]["message"]["content"] or ""
    except Exception as e:
        print(f"  LLM 调用失败：{e}")
        return ""


def parse_numbered_list(text: str) -> List[str]:
    """解析带序号列表，返回字符串列表"""
    lines = text.strip().split("\n")
    results = []
    for line in lines:
        line = re.sub(r"^\s*\d+[\.、\)\]\-]\s*", "", line).strip()
        line = line.strip('"\'')
        if line:
            results.append(line)
    return results


def deduplicate(sentences: List[str], min_len: int = 3, max_len: int = 60) -> List[str]:
    """去重 + 长度过滤"""
    seen = set()
    out = []
    for s in sentences:
        s = s.strip()
        if not s or len(s) < min_len or len(s) > max_len:
            continue
        key = s.replace(" ", "").replace("，", ",").replace("。", ".")
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


# ============================================================
# 读取 / 解析 classifier.py 中的示例字典
# ============================================================
CLASSIFIER_PATH = os.path.join(os.path.dirname(__file__), "classifier.py")
BACKUP_PATH = CLASSIFIER_PATH + ".bak"

# intent_seeds.py 与 classifier.py 的字典命名不一致（历史原因），
# 写回时按此映射定位 classifier 中的目标字典；不在表内的名字按同名查找。
CLASSIFIER_DICT_ALIASES = {
    "COMPOSITIVE_OVERVIEW_STATUS_EXAMPLES": "COMPOSITIVE_OVERVIEW_EXAMPLES",
    "COMPOSITIVE_VEHICLE_STATUS_EXAMPLES": "VEHICLE_STATUS_EXAMPLES",
    "COMPOSITIVE_ENERGY_STATUS_EXAMPLES": "ENERGY_STATUS_EXAMPLES",
    "EMERGENCY_SECURITY_STATUS_EXAMPLES": "SECURITY_STATUS_EXAMPLES",
    "OPERATION_DINING_STATUS_EXAMPLES": "CANTEEN_STATUS_EXAMPLES",
    "OPERATION_MEETING_STATUS_EXAMPLES": "MEETING_STATUS_EXAMPLES",
    "SERVICES_INFORMATION_STATUS_EXAMPLES": "INFORMATION_STATUS_EXAMPLES",
    "SERVICES_DEVICE_STATUS_EXAMPLES": "DEVICE_STATUS_EXAMPLES",
}


def resolve_classifier_dict_name(dict_name: str) -> str:
    """种子字典名 → classifier.py 中的字典名"""
    return CLASSIFIER_DICT_ALIASES.get(dict_name, dict_name)


def extract_examples_from_classifier() -> Dict[str, Dict[str, List[str]]]:
    """
    用 AST 安全解析 classifier.py，提取所有 *_EXAMPLES 示例字典。
    返回结构：{ dict_name: { sub_type: [examples] } }
    """
    with open(CLASSIFIER_PATH, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source)
    result: Dict[str, Dict[str, List[str]]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id.endswith("_EXAMPLES"):
                dict_name = target.id
                value = ast.literal_eval(node.value)
                result[dict_name] = value

    return result


# ============================================================
# 核心生成逻辑
# ============================================================
def generate_for_sub_type(
    intent_name: str,
    intent_desc: str,
    sub_type: str,
    sub_type_desc: str,
    existing: List[str],
) -> List[str]:
    """为单个子类型生成示例，支持无种子/扩写两种模式"""
    all_generated = []
    mode_label = "无种子生成" if not existing else "扩写"
    print(f"  [{mode_label}] {intent_name}/{sub_type}")

    for style in STYLES:
        prompt = build_prompt(
            intent_name=intent_name,
            intent_desc=intent_desc,
            sub_type=sub_type,
            sub_type_desc=sub_type_desc,
            style_name=style["name"],
            style_desc=style["desc"],
            existing_examples=existing,
            count=8,
        )
        raw = call_llm(prompt, temperature=0.85)
        generated = parse_numbered_list(raw)
        print(f"    [{style['name']}] 生成 {len(generated)} 条")
        all_generated.extend(generated)

    # 合并 + 去重；目标数不低于已有条数，避免把人工沉淀的长字典截短
    merged = deduplicate(existing + all_generated)
    target = max(SUB_TYPE_TARGET_COUNT, len(existing))
    if len(merged) > target:
        merged = merged[:target]

    print(f"    => 最终 {len(merged)} 条")
    return merged


def generate_all_examples(only: List[str] = None) -> Dict[str, Dict[str, List[str]]]:
    """生成所有意图所有子类型的示例；only 非空时只处理指定字典名"""
    seeds = {k: v for k, v in INTENT_SEEDS.items() if not only or k in only}
    existing_dicts = extract_examples_from_classifier() if not SEEDLESS_MODE or MERGE_EXISTING else {}
    expanded: Dict[str, Dict[str, List[str]]] = {}

    for dict_name, seed in seeds.items():
        intent_desc = seed.pop("_desc", dict_name)
        print(f"\n处理意图：{dict_name} - {intent_desc}")
        expanded[dict_name] = {}

        # 写回目标按映射表定位（intent_seeds 与 classifier 字典命名可能不同）
        classifier_dict_name = resolve_classifier_dict_name(dict_name)
        existing_sub_types = existing_dicts.get(classifier_dict_name, {})
        if not existing_sub_types:
            print(f"  警告：classifier.py 中未找到 {classifier_dict_name}，结果仅存 JSON 不写回")

        for sub_type, sub_type_desc in seed.items():
            if existing_sub_types and sub_type not in existing_sub_types:
                # classifier 已有该意图字典但没有这个子类型（没有路由），
                # 跳过不生成，避免产出无法参与分类的语料、浪费 LLM 调用
                print(f"  跳过 {sub_type}：classifier.{classifier_dict_name} 中无该子类型")
                continue
            existing = existing_sub_types.get(sub_type, []) if MERGE_EXISTING else []
            expanded[dict_name][sub_type] = generate_for_sub_type(
                intent_name=dict_name.replace("_EXAMPLES", "").replace("_", " "),
                intent_desc=intent_desc,
                sub_type=sub_type,
                sub_type_desc=sub_type_desc,
                existing=existing,
            )

    return expanded


# ============================================================
# 写回 classifier.py
# ============================================================
def build_python_dict_str(name: str, data: Dict[str, List[str]]) -> str:
    """把字典格式化成 classifier.py 风格的 Python 代码字符串"""
    lines = [f"{name} = {{"]
    for sub_type, examples in data.items():
        lines.append(f"    # {sub_type}")
        lines.append(f'    "{sub_type}": [')
        for ex in examples:
            escaped = ex.replace('"', '\\"')
            lines.append(f'        "{escaped}",')
        lines.append("    ],")
        lines.append("")
    lines[-1] = "}"
    return "\n".join(lines)


def patch_classifier(expanded: Dict[str, Dict[str, List[str]]]):
    """把生成后的字典写回 classifier.py（按映射表定位目标字典，保留 classifier 独有子类型）"""
    with open(CLASSIFIER_PATH, "r", encoding="utf-8") as f:
        source = f.read()

    # 备份
    shutil.copy2(CLASSIFIER_PATH, BACKUP_PATH)
    print(f"\n已备份原文件到：{BACKUP_PATH}")

    # classifier 当前内容，用于合并：未处理的子类型原样保留，不丢人工沉淀的语料
    classifier_dicts = extract_examples_from_classifier()

    for dict_name, data in expanded.items():
        target_name = resolve_classifier_dict_name(dict_name)
        tree = ast.parse(source)
        target_node = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == target_name
            ):
                target_node = node
                break

        if not target_node:
            # 如果 classifier.py 中没有该字典，跳过或追加
            print(f"警告：classifier.py 中未找到 {target_name}，跳过")
            continue

        # 只覆盖两边都有的子类型，classifier 独有的子类型保持原样
        merged = dict(classifier_dicts.get(target_name, {}))
        for sub_type, examples in data.items():
            if sub_type in merged:
                merged[sub_type] = examples
            else:
                print(f"  跳过写回 {sub_type}：classifier.{target_name} 中无该子类型")

        start_lineno = target_node.lineno - 1
        end_lineno = target_node.end_lineno

        new_dict_str = build_python_dict_str(target_name, merged)

        source_lines = source.split("\n")
        source_lines = (
            source_lines[:start_lineno]
            + [new_dict_str]
            + source_lines[end_lineno:]
        )
        source = "\n".join(source_lines)

    with open(CLASSIFIER_PATH, "w", encoding="utf-8") as f:
        f.write(source)

    print(f"已更新：{CLASSIFIER_PATH}")


def save_expanded_json(expanded: Dict[str, Dict[str, List[str]]]):
    """同时保存一份 JSON 方便人工检查"""
    output_dir = os.path.join(os.path.dirname(__file__), "generated_examples")
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"expanded_examples_{datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(expanded, f, ensure_ascii=False, indent=2)
    print(f"已保存 JSON 供检查：{path}")


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    # 命令行可选：只生成指定意图（字典名，即 ALL_INTENT_SEEDS 的 key）
    only = sys.argv[1:]
    invalid = [k for k in only if k not in INTENT_SEEDS]
    if invalid:
        print(f"未知的意图字典名：{invalid}\n可选值：")
        for k in INTENT_SEEDS:
            desc = INTENT_SEEDS[k].get("_desc", "")
            print(f"  {k}  # {desc}")
        sys.exit(1)

    print("=" * 60)
    print("意图示例语料生成 / 扩充脚本")
    print("=" * 60)
    print(f"模式：{'无种子生成' if SEEDLESS_MODE else '基于已有示例扩写'}")
    print(f"合并已有示例：{'是' if MERGE_EXISTING else '否'}")
    print(f"目标模型：{LLM_MODEL}")
    print(f"每个子类型目标数量：{SUB_TYPE_TARGET_COUNT}")
    if only:
        print(f"本次只生成：{only}")
    print()

    expanded = generate_all_examples(only=only or None)
    save_expanded_json(expanded)
    patch_classifier(expanded)

    print("\n全部完成。")
