# -*- coding: utf-8 -*-
"""图定义:数据模型 + YAML 解析 + 零成本校验。

校验全部发生在花钱之前(红线 ③):格式、节点类型封闭清单、边引用、
条件边分组、路由字段、入口、连通性、死环。任何一条不过,SpecError
直接指出哪一行哪个字段。

YAML 是图的唯一真相(红线 ②):模型写、人能看能改、界面只渲染它。
"""
import json
import math
import re
from dataclasses import dataclass, field, asdict

import yaml

# 封闭清单(红线 ①):加新类型要写 Python,不能在 YAML 里发明。
# llm    :调模型供应商(带失败链与假成功检测)
# human  :不调模型,暂停等人批准
# research / coding_agent:仅可经显式启用的受控本机 runner 执行；未启用时
#         fail-closed。该 runner 是同用户进程，不是 OS 沙箱。
NODE_TYPES = frozenset({"llm", "human", "research", "coding_agent"})
_AGENT_TYPES = frozenset({"research", "coding_agent"})

_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
# Windows 保留设备名:节点 id 会成为产物文件名(<node>.input/.output/.diff),
# id 首个点分量为 CON/PRN/AUX/NUL/COM1-9/LPT1-9 时,Windows 把整个文件名
# 当作设备访问——exists() 恒为 True,write-once 命名会误判"冲突无法消解"。
# 在校验期拒绝,而不是等到运行中炸出 IntegrityError。
_WIN_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)})
_MODEL_REF_RE = re.compile(r"^[A-Za-z0-9_-]+:[A-Za-z0-9_.\-]+$")
DEFAULT_ROUTE_FIELD = "verdict"
DEFAULT_MAX_ITERATIONS = 3

# YAML 先于业务校验接触不可信输入；这些固定上限约束 compose 的最坏成本。
_MAX_YAML_BYTES = 1 * 1024 * 1024
_MAX_COMPOSE_NODES = 20_000
_MAX_YAML_DEPTH = 64
_MAX_COLLECTION_ITEMS = 5_000


def spec_fingerprint(spec: "WorkflowSpec") -> str:
    """spec 的规范化指纹。run_started 记录它,续跑时校验——
    不允许拿改过的图恢复旧 run(那会产出混合拓扑的假账本)。"""
    import hashlib

    def _node_payload(n) -> dict:
        d = asdict(n)
        # on_error 只在非默认时进指纹:stop 是既有行为,加了字段不该让
        # 旧图的指纹变化(续跑/批复/预检身份都会校验指纹)
        if d.get("on_error") in (None, "stop"):
            d.pop("on_error", None)
        if not d.get("imports"):
            d.pop("imports", None)
        return d

    payload = {
        "name": spec.name,
        "nodes": [_node_payload(n) for n in spec.nodes],
        "edges": [asdict(e) for e in spec.edges],
        "entry": spec.entry,
        "guards": asdict(spec.guards),
    }
    # entries 只在非空时进指纹:单入口图的指纹与旧版一致,
    # 引擎升级不会误杀旧 run 的续跑/批复(M4 审查🟠5)
    if spec.entries:
        payload["entries"] = list(spec.entries)
    # summary 同理只在配置时进指纹:未总结的图与旧版指纹完全一致
    if spec.summary is not None:
        payload["summary"] = asdict(spec.summary)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def spec_to_snapshot(spec: "WorkflowSpec") -> dict:
    """spec 的可序列化快照。execute_graph 把它落进 run 目录,
    批复/续跑用它——不依赖 workflows/ 里的 YAML 当时还在不在。"""
    return {
        "name": spec.name,
        "description": spec.description,
        "nodes": [asdict(n) for n in spec.nodes],
        "edges": [asdict(e) for e in spec.edges],
        "entry": spec.entry,
        "entries": list(spec.entries),
        "guards": asdict(spec.guards),
        "summary": asdict(spec.summary) if spec.summary is not None else None,
        "meta": spec.meta.as_dict(),   # 展示用;旧快照没有此键也能恢复
    }


def spec_from_snapshot(data: dict, *, source: str = "snapshot") -> "WorkflowSpec":
    """从快照重建 spec(走同一套结构校验,快照损坏会大声失败)。"""
    nodes = []
    for ni, n in enumerate(data["nodes"]):
        # asdict 会把 tuple 序列化成 list;空 list 归一为空 tuple
        raw_imports = list(n.get("imports") or [])
        imports = _parse_imports(raw_imports,
                                 where=f"{source} nodes[{ni}]")
        nodes.append(NodeSpec(**{**n, "imports": imports}))
    edges = [EdgeSpec(**e) for e in data["edges"]]
    raw_meta = data.get("meta") or {}
    # 快照里的 meta 已经过一轮校验;再用封闭解析兜一遍(损坏会大声失败)
    meta = _parse_meta(raw_meta if isinstance(raw_meta, dict) else None,
                       where=f"{source} meta")
    spec = WorkflowSpec(
        name=data["name"], description=data.get("description", ""),
        nodes=nodes, edges=edges, entry=data.get("entry", ""),
        guards=Guards(**data.get("guards", {})),
        entries=tuple(data.get("entries", ()) or ()),
        summary=_parse_summary(data.get("summary"), source=source),
        meta=meta,
    )
    validate_spec(spec, source=source)
    return spec


def format_spec_error(message: str, *, path: str | None = None,
                      line: int | None = None,
                      column: int | None = None) -> str:
    """统一渲染规格错误；调用方仍可单独读取结构化字段。"""
    details = []
    if path is not None:
        details.append(f"path {path}")
    if line is not None:
        details.append(f"line {line}")
    if column is not None:
        details.append(f"column {column}")
    return f"{message} ({', '.join(details)})" if details else message


class SpecError(Exception):
    """图定义不合法。零成本拒绝:发生在任何事件与任何模型调用之前。"""

    def __init__(self, message: str, path: str | None = None,
                 line: int | None = None, column: int | None = None):
        super().__init__(message)
        self.message = message
        self.path = path
        self.line = line
        self.column = column

    def __str__(self) -> str:
        return format_spec_error(self.message, path=self.path, line=self.line,
                                 column=self.column)


@dataclass(frozen=True)
class _SourceLocation:
    line: int
    column: int


_SourcePath = tuple[str | int, ...]
_SourceMarks = dict[_SourcePath, _SourceLocation]


def _canonical_path(path: _SourcePath) -> str:
    if not path:
        return "$"
    rendered = ""
    for part in path:
        if isinstance(part, int):
            rendered += f"[{part}]"
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", part):
            rendered += ("." if rendered else "") + part
        else:
            rendered += f"[{json.dumps(part, ensure_ascii=False)}]"
    return rendered


class _RestrictedSafeLoader(yaml.SafeLoader):
    """不支持 anchor/alias，并在 compose 时限制节点数、深度和集合规模。"""

    def __init__(self, stream):
        super().__init__(stream)
        self._compose_nodes = 0
        self._compose_depth = 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.events.AliasEvent):
            event = self.peek_event()
            raise SpecError(
                "YAML alias 不受支持",
                line=event.start_mark.line + 1,
                column=event.start_mark.column + 1,
            )
        event = self.peek_event()
        if event.anchor is not None:
            raise SpecError(
                "YAML anchor 不受支持",
                line=event.start_mark.line + 1,
                column=event.start_mark.column + 1,
            )
        self._compose_nodes += 1
        if self._compose_nodes > _MAX_COMPOSE_NODES:
            raise SpecError(
                f"YAML compose 节点数超过上限 {_MAX_COMPOSE_NODES}",
                line=event.start_mark.line + 1,
                column=event.start_mark.column + 1,
            )
        self._compose_depth += 1
        if self._compose_depth > _MAX_YAML_DEPTH:
            raise SpecError(
                f"YAML 嵌套深度超过上限 {_MAX_YAML_DEPTH}",
                line=event.start_mark.line + 1,
                column=event.start_mark.column + 1,
            )
        try:
            return super().compose_node(parent, index)
        finally:
            self._compose_depth -= 1

    def compose_sequence_node(self, anchor):
        node = super().compose_sequence_node(anchor)
        if len(node.value) > _MAX_COLLECTION_ITEMS:
            raise SpecError(
                f"YAML 集合规模超过上限 {_MAX_COLLECTION_ITEMS}",
                line=node.start_mark.line + 1,
                column=node.start_mark.column + 1,
            )
        return node

    def compose_mapping_node(self, anchor):
        node = super().compose_mapping_node(anchor)
        if len(node.value) > _MAX_COLLECTION_ITEMS:
            raise SpecError(
                f"YAML 集合规模超过上限 {_MAX_COLLECTION_ITEMS}",
                line=node.start_mark.line + 1,
                column=node.start_mark.column + 1,
            )
        return node


def _collect_source_marks(node: yaml.Node,
                          loader: _RestrictedSafeLoader) -> _SourceMarks:
    """校验 compose 树并建立本次解析私有的规范路径→源码位置旁路表。"""
    marks: _SourceMarks = {}
    stack: list[tuple[yaml.Node, _SourcePath]] = [(node, ())]
    while stack:
        current, path = stack.pop()
        marks.setdefault(path, _SourceLocation(current.start_mark.line + 1,
                                               current.start_mark.column + 1))
        if isinstance(current, yaml.MappingNode):
            seen: dict[object, yaml.Node] = {}
            children = []
            for key_node, value_node in current.value:
                if (isinstance(key_node, yaml.ScalarNode)
                        and (key_node.tag == "tag:yaml.org,2002:merge"
                             or key_node.value == "<<")):
                    raise SpecError(
                        "YAML merge key '<<' 不受支持",
                        path=_canonical_path(path + ("<<",)),
                        line=key_node.start_mark.line + 1,
                        column=key_node.start_mark.column + 1,
                    )
                try:
                    key = loader.construct_object(key_node, deep=True)
                    hash(key)
                except (TypeError, ValueError, yaml.YAMLError):
                    raise SpecError(
                        "YAML mapping 的键必须是可比较的标量",
                        path=_canonical_path(path),
                        line=key_node.start_mark.line + 1,
                        column=key_node.start_mark.column + 1,
                    ) from None
                if key in seen:
                    key_part = (str(key_node.value) if isinstance(key_node, yaml.ScalarNode)
                                else "<non-scalar-key>")
                    raise SpecError(
                        f"YAML mapping 包含重复键 {key_part!r}",
                        path=_canonical_path(path + (key_part,)),
                        line=key_node.start_mark.line + 1,
                        column=key_node.start_mark.column + 1,
                    )
                seen[key] = key_node
                if isinstance(key_node, yaml.ScalarNode):
                    child = path + (str(key_node.value),)
                    marks[child] = _SourceLocation(key_node.start_mark.line + 1,
                                                   key_node.start_mark.column + 1)
                    children.append((value_node, child))
            stack.extend(reversed(children))
        elif isinstance(current, yaml.SequenceNode):
            stack.extend((value_node, path + (index,))
                         for index, value_node in reversed(list(enumerate(current.value))))
    return marks


def _load_yaml_with_marks(text: str) -> tuple[object, _SourceMarks]:
    """受限 SafeLoader 生命周期内构造值与 parse-local mark 旁路表。"""
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SpecError(
            "YAML 输入包含无法编码为 UTF-8 的 Unicode 字符",
            line=text.count("\n", 0, exc.start) + 1,
            column=exc.start - text.rfind("\n", 0, exc.start),
        ) from None
    size = len(encoded)
    if size > _MAX_YAML_BYTES:
        raise SpecError(f"YAML 输入超过字节上限 {_MAX_YAML_BYTES}")
    loader = _RestrictedSafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None, {}
        marks = _collect_source_marks(node, loader)
        return loader.construct_document(node), marks
    finally:
        loader.dispose()


_NESTED_FIELDS = {
    "output_schema": frozenset({"required"}),
    "requires": frozenset({"workdir", "human_approval"}),
}


def _field_error_path(error: SpecError, raw: object,
                      allowed: frozenset[str], base: _SourcePath) -> _SourcePath:
    """不改旧消息的前提下，把字段级解析错误归到最具体的 YAML 路径。"""
    if not isinstance(raw, dict):
        return base
    if "未知字段" in error.message:
        unknown = sorted((key for key in raw if isinstance(key, str)
                          and key not in allowed))
        if unknown:
            return base + (unknown[0],)
    for field_name in sorted(allowed, key=len, reverse=True):
        if field_name not in error.message:
            continue
        path = base + (field_name,)
        nested = raw.get(field_name)
        nested_allowed = _NESTED_FIELDS.get(field_name)
        if isinstance(nested, dict) and nested_allowed is not None:
            if "未知字段" in error.message:
                unknown = sorted(key for key in nested
                                 if isinstance(key, str)
                                 and key not in nested_allowed)
                if unknown:
                    return path + (unknown[0],)
            for nested_name in sorted(nested_allowed, key=len, reverse=True):
                if f"{field_name}.{nested_name}" in error.message:
                    return path + (nested_name,)
        return path
    return base


def _node_error_path(error: SpecError, raw: object,
                     base: _SourcePath) -> _SourcePath:
    """节点嵌套块的错误落到具体键，数组元素错误继续落到具体下标。"""
    if isinstance(raw, dict) and "未知字段" in error.message:
        unknown = sorted(key for key in raw
                         if isinstance(key, str) and key not in _NODE_FIELDS)
        if unknown:
            return base + (unknown[0],)
    if isinstance(raw, dict) and "output_schema" in error.message:
        schema = raw.get("output_schema")
        schema_path = base + ("output_schema",)
        if not isinstance(schema, dict):
            return schema_path
        if "未知字段" in error.message:
            unknown = sorted(key for key in schema
                             if isinstance(key, str) and key != "required")
            if unknown:
                return schema_path + (unknown[0],)
        if "required" in error.message:
            required = schema.get("required")
            required_path = schema_path + ("required",)
            if isinstance(required, list):
                for index, value in enumerate(required):
                    if not isinstance(value, str) or not value:
                        return required_path + (index,)
            return required_path
        return schema_path
    return _field_error_path(error, raw, _NODE_FIELDS, base)


def _meta_error_path(error: SpecError, raw: object) -> _SourcePath:
    """meta.requires 的形状、未知字段和值错误均定位到最深叶路径。"""
    base = ("meta",)
    if isinstance(raw, dict) and "meta.requires" in error.message:
        requires = raw.get("requires")
        requires_path = base + ("requires",)
        if not isinstance(requires, dict):
            return requires_path
        if "未知字段" in error.message:
            unknown = sorted(key for key in requires if isinstance(key, str)
                             and key not in {"workdir", "human_approval"})
            if unknown:
                return requires_path + (unknown[0],)
        for field_name in ("human_approval", "workdir"):
            if field_name in error.message:
                return requires_path + (field_name,)
        return requires_path
    return _field_error_path(error, raw, _META_FIELDS, base)


def _located_error(message: str, path: _SourcePath | None,
                   marks: _SourceMarks | None = None, *,
                   use_mark: bool = True,
                   fallback_paths: tuple[_SourcePath, ...] = ()) -> SpecError:
    location = None
    if use_mark and marks is not None:
        for candidate in ((path,) if path is not None else ()) + fallback_paths:
            location = marks.get(candidate)
            if location is not None:
                break
    return SpecError(
        message,
        path=_canonical_path(path) if path is not None else None,
        line=location.line if location is not None else None,
        column=location.column if location is not None else None,
    )


def _relocate_error(error: SpecError, path: _SourcePath | None,
                    marks: _SourceMarks | None = None, *,
                    fallback_paths: tuple[_SourcePath, ...] = ()) -> SpecError:
    if error.path is not None or error.line is not None or error.column is not None:
        return error
    return _located_error(error.message, path, marks,
                          fallback_paths=fallback_paths)


@dataclass(frozen=True)
class Guards:
    # None = 未显式设置(有环时校验会拒绝;运行时生效值用 effective_max_iterations)
    max_iterations: int | None = None
    max_cost_usd: float | None = None   # M2 生效(需先建 pricing.json)
    timeout_s: float | None = None

    def __post_init__(self) -> None:
        # 编程构造和快照恢复也必须与 YAML 走同一条强校验，不能只守解析入口。
        if self.max_iterations is not None and (
                not isinstance(self.max_iterations, int)
                or isinstance(self.max_iterations, bool)
                or self.max_iterations < 1):
            raise SpecError(
                f"guards.max_iterations 必须是 ≥1 的整数,得到 {self.max_iterations!r}")
        for name, value in (("max_cost_usd", self.max_cost_usd),
                            ("timeout_s", self.timeout_s)):
            if value is not None and (
                    not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value <= 0):
                raise SpecError(f"guards.{name} 必须是正的有限数,得到 {value!r}")

    @property
    def effective_max_iterations(self) -> int:
        return self.max_iterations if self.max_iterations is not None else DEFAULT_MAX_ITERATIONS

    @classmethod
    def from_dict(cls, d: dict | None) -> "Guards":
        if d is None:
            return cls()
        if not isinstance(d, dict):
            raise SpecError("guards 必须是映射")
        unknown = set(d) - {"max_iterations", "max_cost_usd", "timeout_s"}
        if unknown:
            raise SpecError(f"guards 里有未知字段:{sorted(unknown)}。"
                            f"可用:max_iterations / max_cost_usd / timeout_s")
        kwargs = {}
        if "max_iterations" in d:
            v = d["max_iterations"]
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                raise SpecError(f"guards.max_iterations 必须是 ≥1 的整数,得到 {v!r}")
            kwargs["max_iterations"] = v
        if "max_cost_usd" in d:
            v = d["max_cost_usd"]
            # isfinite:YAML 里 .nan 会变成 float('nan'),nan<=0 为 False,
            # 不拦就是一条永不触发的假守卫
            if not isinstance(v, (int, float)) or isinstance(v, bool) \
                    or not math.isfinite(v) or v <= 0:
                raise SpecError(f"guards.max_cost_usd 必须是正的有限数,得到 {v!r}")
            kwargs["max_cost_usd"] = float(v)
        if "timeout_s" in d:
            v = d["timeout_s"]
            if not isinstance(v, (int, float)) or isinstance(v, bool) \
                    or not math.isfinite(v) or v <= 0:
                raise SpecError(f"guards.timeout_s 必须是正的有限数,得到 {v!r}")
            kwargs["timeout_s"] = float(v)
        return cls(**kwargs)


@dataclass(frozen=True)
class SummarySpec:
    """图级 opt-in 总结配置(S1):run_done 前一次总结调用。

    语义字段,进规格指纹与快照——它改变执行(多一次计费调用),
    不像 meta 那样只作展示。默认缺省 = 不总结。
    """
    model: str                    # 总结调用的模型引用(与节点 model 同一命名法)
    prompt_hint: str = ""         # 追加给总结员的用户要求(可选)


_SUMMARY_FIELDS = frozenset({"model", "prompt_hint"})


@dataclass(frozen=True)
class ArtifactImport:
    """节点级产物导入声明(P7):从某个终态 run 复制一份昂贵上游结果。

    校验期只做形状;源 run 的存在性/终态/事件 provenance 在启动准入
    (execute_graph 的源 run stable lock 内)核验——spec 校验必须零成本、
    不碰文件系统。非默认(空)时才进指纹:旧图指纹零变化。
    """
    run: str                      # 源 run id
    name: str                     # 源逻辑名(<producer>.output / .diff / .error)

    def as_dict(self) -> dict:
        return {"run": self.run, "name": self.name}


_IMPORT_ENTRY_FIELDS = frozenset({"run", "name"})
_IMPORT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.(output|diff|error)$")


def _parse_imports(raw: object, *, where: str) -> tuple[ArtifactImport, ...]:
    if raw is None or raw == []:
        return ()
    if not isinstance(raw, list):
        raise SpecError(f"{where} 必须是数组或省略")
    out: list[ArtifactImport] = []
    seen: set[tuple[str, str]] = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SpecError(f"{where}[{i}] 必须是映射(run, name)")
        unknown = {k for k in entry if isinstance(k, str)} - _IMPORT_ENTRY_FIELDS
        if unknown:
            raise SpecError(f"{where}[{i}] 有未知字段:{sorted(unknown)}。"
                            f"可用:run/name")
        run = entry.get("run")
        name = entry.get("name")
        if not isinstance(run, str) or not _NODE_ID_RE.match(run):
            raise SpecError(f"{where}[{i}].run 必须是合法 run id")
        if not isinstance(name, str) or _IMPORT_NAME_RE.match(name) is None:
            raise SpecError(
                f"{where}[{i}].name 必须形如 '<producer>.output' / '.diff' / "
                f"'.error',得到 {name!r}")
        key = (run, name)
        if key in seen:
            raise SpecError(f"{where} 里 ({run}, {name}) 重复声明")
        seen.add(key)
        out.append(ArtifactImport(run=run, name=name))
    return tuple(out)


def _parse_summary(d: object, *, source: str) -> SummarySpec | None:
    if d is None:
        return None
    if not isinstance(d, dict):
        raise SpecError(
            f"{source} 的 summary 必须是映射(model[, prompt_hint]),"
            f"实际是 {type(d).__name__}")
    unknown = {k for k in d if isinstance(k, str)} - _SUMMARY_FIELDS
    if unknown:
        raise SpecError(f"summary 里有未知字段:{sorted(unknown)}。"
                        f"可用:model/prompt_hint")
    model = d.get("model")
    if not isinstance(model, str) or not model.strip():
        raise SpecError("summary.model 必须是非空字符串(总结调用的模型引用)")
    hint = d.get("prompt_hint", "")
    if not isinstance(hint, str):
        raise SpecError("summary.prompt_hint 必须是字符串")
    return SummarySpec(model=model.strip(), prompt_hint=hint)


# P3 失败策略与保留路由键(锚点:on_error 闭合枚举;branch 只走 __failed__)
ON_ERROR_VALUES = ("stop", "continue", "branch")
FAILED_EDGE_KEY = "__failed__"


@dataclass(frozen=True)
class NodeSpec:
    id: str
    type: str                                   # 必须命中 NODE_TYPES
    model: str                                  # "供应商id:模型id";非 llm 类型留空
    prompt: str
    consumes: list[str] = field(default_factory=lambda: ["task"])
    required_fields: list[str] | None = None    # output_schema.required
    fallback: list[str] = field(default_factory=list)
    route_field: str = DEFAULT_ROUTE_FIELD      # 条件边按这个字段的值查表
    workdir: str = ""                           # coding_agent:目标项目目录(绝对路径)
    max_turns: int = 12                         # agent 节点的 CLI 轮次上限
    # ── M4 节点参数(全部可选;A9:每个都必须有"真的生效"的测试) ──
    max_output_tokens: int | None = None        # llm:覆盖供应商默认
    thinking: str | None = None                 # llm:low/medium/high/xhigh 四档
    temperature: float | None = None            # llm
    seed: int | None = None                     # llm:各家是否尊重未验证,勿承诺可复现
    writable: bool = True                       # coding_agent:False=只读且不采集改动
    allow_web: bool | None = None               # agent:默认关，必须在 YAML 显式开启
    allowed_paths: list[str] = field(default_factory=list)  # 仅不可写 agent:附加目录
    timeout_s: float | None = None              # 全部:节点级覆盖 guards.timeout_s
    retry: int = 0                              # 全部:同模型传输失败重试(与失败链正交)
    # P3 失败策略:内容类失败(候选全部失败)可 stop/continue/branch;
    # 治理类异常永不可吞。branch 走保留键 __failed__ 的条件边。
    on_error: str = "stop"
    # P7 产物导入:从终态 run 复制上游结果作为本节点输入的替代来源。
    # 空元组 = 未声明;仅非空进指纹与快照语义有效值。
    imports: tuple[ArtifactImport, ...] = ()

    @property
    def output_name(self) -> str:
        return f"{self.id}.output"


@dataclass(frozen=True)
class EdgeSpec:
    source: str
    target: str            # "END" 表示终点
    when: str | None = None  # 条件边的匹配值(对 source 的 route_field)


@dataclass(frozen=True)
class WorkflowMeta:
    """工作流元数据(PLAN-v3 §2.4)。只服务于发现/说明/管理,不参与执行:
    改 meta 不改指纹,不影响旧 run。字段是封闭清单,未知字段拒绝。"""
    title: str = ""
    description: str = ""
    kind: str = "custom"          # example | template | custom
    category: str = ""            # 研究/决策/代码/审批/文档/其他(展示分组)
    tags: tuple[str, ...] = ()
    estimated_calls: int | str | None = None
    requires_workdir: bool = False
    requires_human: bool = False
    example_task: str = ""

    def as_dict(self) -> dict:
        d = {"kind": self.kind}
        if self.category:
            d["category"] = self.category
        if self.title:
            d["title"] = self.title
        if self.description:
            d["description"] = self.description
        if self.tags:
            d["tags"] = list(self.tags)
        if self.estimated_calls is not None:
            d["estimated_calls"] = self.estimated_calls
        d["requires"] = {"workdir": self.requires_workdir,
                         "human_approval": self.requires_human}
        if self.example_task:
            d["example_task"] = self.example_task
        return d


_META_KINDS = frozenset({"example", "template", "custom"})
_META_CATEGORIES = frozenset({"research", "decision", "code", "approval",
                              "document", "other"})
_META_CATEGORY_LABELS = {"research": "研究", "decision": "决策", "code": "代码",
                         "approval": "审批", "document": "文档", "other": "其他"}


def _parse_meta(raw, *, where: str, spec: "WorkflowSpec | None" = None) -> WorkflowMeta:
    """meta 块解析:封闭字段 + 长度/数量上限。坏值零成本拒绝。"""
    if raw is None:
        return WorkflowMeta()
    if not isinstance(raw, dict):
        raise SpecError(f"{where} 的 meta 必须是映射")
    unknown = set(raw) - {"title", "description", "kind", "category", "tags",
                          "estimated_calls", "requires", "example_task"}
    if unknown:
        raise SpecError(f"{where} meta 里有未知字段:{sorted(unknown)}。"
                        f"可用:title/description/kind/category/tags/"
                        f"estimated_calls/requires/example_task")
    kw: dict = {}
    if "title" in raw:
        v = raw["title"]
        if not isinstance(v, str) or not v.strip() or len(v) > 60:
            raise SpecError(f"{where} meta.title 必须是 1–60 字符的字符串")
        kw["title"] = v.strip()
    if "description" in raw:
        v = raw["description"]
        if not isinstance(v, str) or len(v) > 200:
            raise SpecError(f"{where} meta.description 必须是 ≤200 字符的字符串")
        kw["description"] = v.strip()
    if "kind" in raw:
        v = raw["kind"]
        if v not in _META_KINDS:
            raise SpecError(f"{where} meta.kind 必须是 {sorted(_META_KINDS)} 之一,"
                            f"得到 {v!r}")
        kw["kind"] = v
    if "category" in raw:
        v = raw["category"]
        # 空串 = 未分组(快照往返/手写都可能出现),不算错
        if v != "" and v not in _META_CATEGORIES:
            raise SpecError(f"{where} meta.category 必须是 "
                            f"{sorted(_META_CATEGORIES)} 之一,得到 {v!r}")
        kw["category"] = v
    if "tags" in raw:
        v = raw["tags"]
        if (not isinstance(v, list) or len(v) > 8
                or not all(isinstance(t, str) and t.strip() and len(t) <= 16 for t in v)):
            raise SpecError(f"{where} meta.tags 必须是 ≤8 个、每个 ≤16 字符的字符串数组")
        kw["tags"] = tuple(t.strip() for t in v)
    if "estimated_calls" in raw:
        v = raw["estimated_calls"]
        if isinstance(v, bool) or not isinstance(v, (int, str)) or \
                (isinstance(v, int) and v < 1) or \
                (isinstance(v, str) and (not v.strip() or len(v) > 40)):
            raise SpecError(f"{where} meta.estimated_calls 必须是 ≥1 的整数"
                            f"或简短字符串(如「每轮 2 次,上限 3 轮」)")
        kw["estimated_calls"] = v if isinstance(v, str) else int(v)
    if "requires" in raw:
        v = raw["requires"]
        if not isinstance(v, dict):
            raise SpecError(f"{where} meta.requires 必须是映射")
        req_unknown = set(v) - {"workdir", "human_approval"}
        if req_unknown:
            raise SpecError(f"{where} meta.requires 里有未知字段:{sorted(req_unknown)}")
        if "workdir" in v and not isinstance(v["workdir"], bool):
            raise SpecError(f"{where} meta.requires.workdir 必须是布尔值")
        if "human_approval" in v and not isinstance(v["human_approval"], bool):
            raise SpecError(f"{where} meta.requires.human_approval 必须是布尔值")
        kw["requires_workdir"] = bool(v.get("workdir", False))
        kw["requires_human"] = bool(v.get("human_approval", False))
    if "example_task" in raw:
        v = raw["example_task"]
        if not isinstance(v, str) or not v.strip() or len(v) > 400:
            raise SpecError(f"{where} meta.example_task 必须是 1–400 字符的字符串")
        kw["example_task"] = v.strip()
    # 与图结构的一致性:声明了要求但图里没有对应节点 → 提示性错误
    # (fail-closed:示例卡说「需人工批准」而图里没有 human 节点是骗人)
    if spec is not None and "requires" in raw:
        has_human = any(n.type == "human" for n in spec.nodes)
        has_agent = any(n.type == "coding_agent" for n in spec.nodes)
        if kw.get("requires_human") and not has_human:
            raise SpecError(f"{where} meta.requires.human_approval=true,但图里没有 human 节点")
        if kw.get("requires_workdir") and not has_agent:
            raise SpecError(f"{where} meta.requires.workdir=true,但图里没有 coding_agent 节点")
    return WorkflowMeta(**kw)


@dataclass(frozen=True)
class WorkflowSpec:
    name: str
    nodes: list[NodeSpec]
    edges: list[EdgeSpec]
    entry: str                                  # 主入口(向后兼容的单值视图)
    description: str = ""
    guards: Guards = field(default_factory=Guards)
    entries: tuple[str, ...] = ()               # 全部入口(M4 多入口;空=单入口)
    summary: SummarySpec | None = None          # S1 opt-in 总结(run_done 前一次调用)
    meta: WorkflowMeta = field(default_factory=WorkflowMeta)   # 展示用,不进指纹

    def node(self, node_id: str) -> NodeSpec:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    def outgoing(self, node_id: str) -> list[EdgeSpec]:
        return [e for e in self.edges if e.source == node_id]

    def all_entries(self) -> tuple[str, ...]:
        return self.entries or (self.entry,)


# ─────────────────────────── YAML 解析 ───────────────────────────


_TOP_FIELDS = frozenset({"name", "description", "entry", "nodes", "edges",
                         "guards", "summary", "meta"})
_NODE_FIELDS = frozenset({
    "id", "type", "model", "prompt", "consumes", "output_schema", "fallback",
    "route_field", "workdir", "max_turns", "max_output_tokens", "thinking",
    "temperature", "seed", "writable", "allow_web", "allowed_paths",
    "timeout_s", "retry", "on_error", "imports",
})
_EDGE_FIELDS = frozenset({"from", "to", "when"})
_GUARD_FIELDS = frozenset({"max_iterations", "max_cost_usd", "timeout_s"})
_META_FIELDS = frozenset({"title", "description", "kind", "category", "tags",
                          "estimated_calls", "requires", "example_task"})


def spec_from_yaml(text: str, *, source: str = "YAML") -> WorkflowSpec:
    """YAML 文本 → 校验过的 WorkflowSpec。任何问题抛 SpecError,零成本。"""
    try:
        raw, marks = _load_yaml_with_marks(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        message = getattr(e, "problem", None) or str(e)
        raise SpecError(
            f"{source} 不是合法 YAML:{message}",
            line=mark.line + 1 if mark is not None else None,
            column=mark.column + 1 if mark is not None else None,
        ) from e
    if not isinstance(raw, dict):
        raise _located_error(
            f"{source} 的顶层必须是映射(键:name/nodes/edges 等),"
            f"实际是 {type(raw).__name__}", (), marks)
    unknown_top = set(raw) - _TOP_FIELDS
    if unknown_top:
        field = sorted(unknown_top)[0]
        raise _located_error(
            f"{source} 顶层有未知字段:{sorted(unknown_top)}。"
            f"可用:name/description/entry/nodes/edges/guards/summary/meta",
            (field,), marks)

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _located_error(f"{source} 缺少非空的 name", ("name",), marks,
                             fallback_paths=((),))

    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise _located_error(f"{source} 的 nodes 必须是非空数组",
                             ("nodes",), marks)

    nodes: list[NodeSpec] = []
    for i, rn in enumerate(raw_nodes):
        base = ("nodes", i)
        try:
            nodes.append(_parse_node(rn, where=f"{source} nodes[{i}]"))
        except SpecError as e:
            path = _node_error_path(e, rn, base)
            raise _relocate_error(e, path, marks, fallback_paths=(base,)) from e

    raw_edges = raw.get("edges", [])
    if not isinstance(raw_edges, list):
        raise _located_error(f"{source} 的 edges 必须是数组",
                             ("edges",), marks)
    edges: list[EdgeSpec] = []
    for i, re_ in enumerate(raw_edges):
        base = ("edges", i)
        try:
            edges.append(_parse_edge(re_, where=f"{source} edges[{i}]"))
        except SpecError as e:
            path = _field_error_path(e, re_, _EDGE_FIELDS, base)
            raise _relocate_error(e, path, marks, fallback_paths=(base,)) from e

    # entry 接受单值或列表(多入口并行,PLAN-v2 M4)
    raw_entry = raw.get("entry", "")
    if isinstance(raw_entry, list):
        if not raw_entry:
            raise _located_error(f"{source}:entry 列表不能为空",
                                 ("entry",), marks)
        entries = tuple(str(e) for e in raw_entry)
        entry = entries[0]
    else:
        entry = str(raw_entry or "")
        entries = ()

    try:
        guards = Guards.from_dict(raw.get("guards"))
    except SpecError as e:
        path = _field_error_path(e, raw.get("guards"), _GUARD_FIELDS,
                                 ("guards",))
        raise _relocate_error(e, path, marks,
                              fallback_paths=(("guards",),)) from e

    try:
        summary = _parse_summary(raw.get("summary"), source=source)
    except SpecError as e:
        raise _relocate_error(e, ("summary",), marks,
                              fallback_paths=(("summary",),)) from e

    spec = WorkflowSpec(
        name=name.strip(),
        description=str(raw.get("description", "") or ""),
        nodes=nodes,
        edges=edges,
        entry=entry,
        guards=guards,
        entries=entries,
        summary=summary,
    )
    resolved = validate_spec(spec, source=source, _marks=marks)
    # meta 在结构校验之后解析:requires 的一致性检查需要先有 spec
    try:
        meta = _parse_meta(raw.get("meta"), where=f"{source}(工作流 {name.strip()})",
                           spec=spec)
    except SpecError as e:
        path = _meta_error_path(e, raw.get("meta"))
        raise _relocate_error(e, path, marks, fallback_paths=(("meta",),)) from e
    spec = WorkflowSpec(
        name=spec.name, description=spec.description, nodes=spec.nodes,
        edges=spec.edges, entry=spec.entry, guards=spec.guards,
        entries=spec.entries, summary=summary, meta=meta,
    )
    # 把解析出的入口写回。**只有完全没写 entry 时**才推断:
    # 写了单值 entry 就只跑那一条腿(多根图里其余根会因不可达被拒收,
    # 这是指引你显式选择,不是静默多跑);写了列表就按列表。
    if not spec.entry and not spec.entries:
        by_id = {n.id for n in nodes}
        roots = _infer_roots(spec, by_id)
        all_entries = tuple(roots) if len(roots) > 1 else (resolved,)
        spec = WorkflowSpec(
            name=spec.name, description=spec.description, nodes=spec.nodes,
            edges=spec.edges, entry=resolved, guards=spec.guards,
            entries=all_entries, summary=summary, meta=meta,
        )
    return spec


def spec_from_yaml_file(path) -> WorkflowSpec:
    from pathlib import Path
    path = Path(path)
    try:
        # utf-8-sig:Windows 记事本存的 YAML 带 BOM,裸 utf-8 会让 PyYAML
        # 报出误导性的语法错误;-sig 读侧兼容有无 BOM
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as e:
        raise SpecError(f"找不到图定义文件 {path}") from e
    return spec_from_yaml(text, source=str(path))


def _parse_node(rn, *, where: str) -> NodeSpec:
    if not isinstance(rn, dict):
        raise SpecError(f"{where} 必须是映射")
    unknown = set(rn) - {"id", "type", "model", "prompt", "consumes",
                         "output_schema", "fallback", "route_field",
                         "workdir", "max_turns", "max_output_tokens",
                         "thinking", "temperature", "seed", "writable",
                         "allow_web", "allowed_paths", "timeout_s", "retry",
                         "on_error", "imports"}
    if unknown:
        raise SpecError(f"{where} 有未知字段:{sorted(unknown)}。"
                        f"可用:id/type/model/prompt/consumes/output_schema/"
                        f"fallback/route_field/workdir/max_turns/"
                        f"max_output_tokens/thinking/temperature/seed/"
                        f"writable/allow_web/allowed_paths/timeout_s/retry/"
                        f"on_error/imports")

    nid = rn.get("id")
    if not isinstance(nid, str) or not _NODE_ID_RE.match(nid):
        raise SpecError(f"{where} 的 id {nid!r} 不合法(允许字母数字_.-,首字符不能是 . 或 -)")
    if nid.split(".")[0].upper() in _WIN_RESERVED_STEMS:
        raise SpecError(
            f"{where} 的 id {nid!r} 以 Windows 保留设备名开头"
            f"({nid.split('.')[0].upper()}):它会成为产物文件名,在 Windows 上"
            f"被当作设备而不是文件。请换一个 id")

    ntype = rn.get("type")
    if ntype not in NODE_TYPES:
        raise SpecError(
            f"{where}(节点 {nid})的 type {ntype!r} 不在封闭清单里:{sorted(NODE_TYPES)}。"
            f"加新类型要写 Python,不能在图定义里发明"
        )

    model = rn.get("model")
    if model is None:
        model = ""
    if ntype == "llm":
        # model 允许留空(未配置):仓库示例不预填任何机器的供应商,
        # 新用户先看到"待选择";空模型会在运行前校验被拒绝(校验先于花钱)。
        # 非空时必须是合法引用。
        if not isinstance(model, str) or (model and not _MODEL_REF_RE.match(model)):
            raise SpecError(f"{where}(节点 {nid})的 model 必须形如 '供应商id:模型id'"
                            f"(可留空表示未配置),得到 {model!r}")
    elif ntype in _AGENT_TYPES:
        # agent 节点的 model 可选:不配用默认;配了须是合法引用
        # (用于选择驱动 claude CLI 的 anthropic 网关)
        if model and not _MODEL_REF_RE.match(model):
            raise SpecError(
                f"{where}(节点 {nid})的 model 必须形如 '供应商id:模型id',得到 {model!r}")
    else:
        if model:
            raise SpecError(
                f"{where}(节点 {nid})的类型 {ntype} 不调模型,model 必须省略"
            )

    workdir = rn.get("workdir", "")
    if ntype == "coding_agent":
        if not isinstance(workdir, str) or not workdir:
            raise SpecError(
                f"{where}(节点 {nid})是 coding_agent,必须给 workdir:"
                f"要改的目标项目的绝对路径。改动只发生在它的隔离副本里")
        from pathlib import Path as _P
        if not _P(workdir).is_dir():
            raise SpecError(
                f"{where}(节点 {nid})的 workdir {workdir!r} 不是存在的目录"
            )
    elif workdir:
        raise SpecError(f"{where}(节点 {nid})只有 coding_agent 能用 workdir")

    max_turns = rn.get("max_turns", 12)
    if ntype in _AGENT_TYPES:
        if not isinstance(max_turns, int) or isinstance(max_turns, bool) \
                or not 1 <= max_turns <= 64:
            raise SpecError(f"{where}(节点 {nid})的 max_turns 必须是 1–64 的整数")
    elif "max_turns" in rn:
        raise SpecError(f"{where}(节点 {nid})只有 agent 类节点能用 max_turns")

    # ── M4 参数解析与类型专属校验(A9:每个都必须有生效测试) ──
    def _only(types, field_name):
        if ntype not in types:
            raise SpecError(f"{where}(节点 {nid})的 {field_name} 只适用于 "
                            f"{'/'.join(sorted(types))} 节点")

    max_output_tokens = rn.get("max_output_tokens")
    if max_output_tokens is not None:
        _only({"llm"}, "max_output_tokens")
        if not isinstance(max_output_tokens, int) or isinstance(max_output_tokens, bool) \
                or max_output_tokens < 1:
            raise SpecError(f"{where}(节点 {nid})的 max_output_tokens 必须是正整数")

    thinking = rn.get("thinking")
    if thinking is not None:
        _only({"llm"}, "thinking")
        from atlas.thinking import TIERS, model_capability
        if thinking not in TIERS:
            raise SpecError(f"{where}(节点 {nid})的 thinking 必须是 {list(TIERS)} 之一,"
                            f"得到 {thinking!r}")
        # 模型未配置时跳过能力检查;选定模型后 preview/运行前会再次校验。
        if model and model_capability(model) == "none":
            raise SpecError(
                f"{where}(节点 {nid}):模型 {model} 没有真实的思考控制"
                f"(实测:参数被网关静默忽略)。删掉 thinking,或换支持思考的模型;"
                f"能力表见 config/capabilities.json")
        if model and model_capability(model) == "budget":
            from atlas.thinking import BUDGET_MAP, provider_default_max_tokens
            budget = BUDGET_MAP[thinking]
            cap = max_output_tokens or provider_default_max_tokens(model) or 8192
            if budget >= cap:
                raise SpecError(
                    f"{where}(节点 {nid}):thinking {thinking} 的思考预算 "
                    f"{budget} 不小于输出上限 {cap}(Anthropic 要求预算 < 上限,"
                    f"网关会直接拒绝)。给该节点加 max_output_tokens: {budget + 4096}")

    temperature = rn.get("temperature")
    if temperature is not None:
        _only({"llm"}, "temperature")
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) \
                or not 0 <= temperature <= 2:
            raise SpecError(f"{where}(节点 {nid})的 temperature 必须在 0–2,得到 {temperature!r}")

    seed = rn.get("seed")
    if seed is not None:
        _only({"llm"}, "seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise SpecError(f"{where}(节点 {nid})的 seed 必须是整数")

    writable = rn.get("writable", True)
    if "writable" in rn:
        _only({"coding_agent"}, "writable")
        if not isinstance(writable, bool):
            raise SpecError(f"{where}(节点 {nid})的 writable 必须是布尔")

    allow_web = rn.get("allow_web")
    if allow_web is not None:
        _only(_AGENT_TYPES, "allow_web")
        if not isinstance(allow_web, bool):
            raise SpecError(f"{where}(节点 {nid})的 allow_web 必须是布尔")
    else:
        allow_web = False   # 网络能力必须在 YAML 中显式开启

    allowed_paths = rn.get("allowed_paths", [])
    if "allowed_paths" in rn:
        _only(_AGENT_TYPES, "allowed_paths")
        if not isinstance(allowed_paths, list) or not all(
                isinstance(pp, str) and pp for pp in allowed_paths):
            raise SpecError(f"{where}(节点 {nid})的 allowed_paths 必须是路径数组")
        if ntype == "coding_agent" and writable and allowed_paths:
            raise SpecError(
                f"{where}(节点 {nid}):writable: true 与 allowed_paths 不能同时使用;"
                "Claude CLI 的 --add-dir 不是只读边界。请设 writable: false，"
                "或删除 allowed_paths")
        from pathlib import Path as _P2
        for pp in allowed_paths:
            if not _P2(pp).is_dir():
                raise SpecError(f"{where}(节点 {nid})的 allowed_paths 里 {pp!r} 不是存在的目录")

    timeout_s = rn.get("timeout_s")
    if timeout_s is not None:
        _only({"llm", "research", "coding_agent"}, "timeout_s")
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) \
                or timeout_s <= 0:
            raise SpecError(f"{where}(节点 {nid})的 timeout_s 必须是正数")

    retry = rn.get("retry", 0)
    if "retry" in rn:
        _only({"llm", "research", "coding_agent"}, "retry")
    if not isinstance(retry, int) or isinstance(retry, bool) or not 0 <= retry <= 10:
        raise SpecError(f"{where}(节点 {nid})的 retry 必须是 0–10 的整数")

    prompt = rn.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise SpecError(f"{where}(节点 {nid})缺少非空的 prompt")

    consumes = rn.get("consumes", ["task"])
    if not isinstance(consumes, list) or not consumes \
            or not all(isinstance(c, str) and c for c in consumes):
        raise SpecError(f"{where}(节点 {nid})的 consumes 必须是非空字符串数组")

    required = None
    if "output_schema" in rn:
        schema = rn["output_schema"]
        if not isinstance(schema, dict):
            raise SpecError(f"{where}(节点 {nid})的 output_schema 必须是映射")
        unknown_s = set(schema) - {"required"}
        if unknown_s:
            raise SpecError(f"{where}(节点 {nid})的 output_schema 里有未知字段:{sorted(unknown_s)}")
        required = schema.get("required")
        if not isinstance(required, list) or not required \
                or not all(isinstance(f, str) and f for f in required):
            raise SpecError(f"{where}(节点 {nid})的 output_schema.required 必须是非空字符串数组")

    fallback = rn.get("fallback", [])
    if not isinstance(fallback, list) or not all(
            isinstance(f, str) and _MODEL_REF_RE.match(f) for f in fallback):
        raise SpecError(f"{where}(节点 {nid})的 fallback 必须是 '供应商id:模型id' 数组")
    if fallback and ntype != "llm":
        raise SpecError(f"{where}(节点 {nid})的类型 {ntype} 没有 fallback(不调模型)")
    if len(fallback) != len(set(fallback)):
        raise SpecError(f"{where}(节点 {nid})的 fallback 必须互不重复")
    if model and model in fallback:
        raise SpecError(f"{where}(节点 {nid})的 fallback 不能包含主模型 {model!r}")

    route_field = rn.get("route_field", DEFAULT_ROUTE_FIELD)
    if not isinstance(route_field, str) or not route_field:
        raise SpecError(f"{where}(节点 {nid})的 route_field 必须是非空字符串")

    on_error = rn.get("on_error", "stop")
    if on_error is None:
        on_error = "stop"
    if not isinstance(on_error, str) or on_error not in ON_ERROR_VALUES:
        raise SpecError(f"{where}(节点 {nid})的 on_error 必须是 "
                        f"{list(ON_ERROR_VALUES)} 之一,得到 {on_error!r}")
    if "on_error" in rn and on_error != "stop":
        # 内容类失败只发生在 llm 节点;其他类型的 on_error 是无效承诺,
        # 校验期拒绝而不是运行期静默忽略
        _only({"llm"}, "on_error")

    try:
        imports = _parse_imports(rn.get("imports"),
                                 where=f"{where}(节点 {nid})")
    except SpecError:
        raise

    return NodeSpec(id=nid, type=ntype, model=model, prompt=prompt,
                    consumes=list(consumes), required_fields=required,
                    fallback=list(fallback), route_field=route_field,
                    workdir=workdir, max_turns=max_turns,
                    max_output_tokens=max_output_tokens, thinking=thinking,
                    temperature=temperature, seed=seed, writable=writable,
                    allow_web=allow_web, allowed_paths=list(allowed_paths),
                    timeout_s=timeout_s, retry=retry, on_error=on_error,
                    imports=imports)


def validate_node_spec(node: NodeSpec, *, where: str = "node") -> NodeSpec:
    """用 YAML 节点的同一套封闭规则校验编程构造/运行时覆盖后的节点。

    运行时覆盖不能只做 dataclass.replace 后交给 validate_spec:后者只校验图结构，
    不会重复 temperature/thinking/timeout 等字段级约束。这里把节点还原为解析器
    接受的形状，再走 _parse_node，保证 YAML 与 Web/MCP 覆盖没有两套规则。
    """
    raw: dict = {
        "id": node.id,
        "type": node.type,
        "prompt": node.prompt,
        "consumes": list(node.consumes),
        "route_field": node.route_field,
    }
    if node.model:
        raw["model"] = node.model
    if node.required_fields:
        raw["output_schema"] = {"required": list(node.required_fields)}
    if node.fallback:
        raw["fallback"] = list(node.fallback)
    if node.type == "coding_agent":
        raw["workdir"] = node.workdir
        raw["writable"] = node.writable
    if node.type in _AGENT_TYPES:
        raw["max_turns"] = node.max_turns
        raw["allow_web"] = node.allow_web
        if node.allowed_paths:
            raw["allowed_paths"] = list(node.allowed_paths)
    if node.type == "llm":
        if node.max_output_tokens is not None:
            raw["max_output_tokens"] = node.max_output_tokens
        if node.thinking is not None:
            raw["thinking"] = node.thinking
        if node.temperature is not None:
            raw["temperature"] = node.temperature
        if node.seed is not None:
            raw["seed"] = node.seed
    if node.type in {"llm", "research", "coding_agent"}:
        if node.timeout_s is not None:
            raw["timeout_s"] = node.timeout_s
        raw["retry"] = node.retry
    if node.on_error != "stop":
        raw["on_error"] = node.on_error
    if node.imports:
        raw["imports"] = [i.as_dict() for i in node.imports]
    return _parse_node(raw, where=where)


def _parse_edge(re_, *, where: str) -> EdgeSpec:
    if not isinstance(re_, dict):
        raise SpecError(f"{where} 必须是映射")
    unknown = set(re_) - {"from", "to", "when"}
    if unknown:
        raise SpecError(f"{where} 有未知字段:{sorted(unknown)}。可用:from/to/when")
    src = re_.get("from")
    tgt = re_.get("to")
    when = re_.get("when")
    if not isinstance(src, str) or not src:
        raise SpecError(f"{where} 缺少非空的 from")
    if not isinstance(tgt, str) or not tgt:
        raise SpecError(f"{where} 缺少非空的 to")
    if when is not None and (not isinstance(when, str) or not when):
        raise SpecError(f"{where} 的 when 必须是非空字符串或省略")
    return EdgeSpec(source=src, target=tgt, when=when)


# ─────────────────────────── 结构校验 ───────────────────────────


def validate_spec(spec: WorkflowSpec, *, source: str = "spec",
                  _marks: _SourceMarks | None = None) -> str:
    """全部零成本。编程构造的 spec 同样要过这里(execute_graph 会调)。

    ``_marks`` 只由本模块的 YAML 入口传入，不存进 spec；编程构造和快照恢复
    仍得到相同的 path/message，只是不带源码 line/column。
    返回解析后的入口节点 id(显式声明优先,否则从「唯一无入边节点」推断)。
    """
    ids = [n.id for n in spec.nodes]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise _located_error(f"{source}:节点 id 重复:{dupes}",
                             ("nodes",), _marks, use_mark=False)
    by_id = {n.id: n for n in spec.nodes}
    node_indexes = {n.id: i for i, n in enumerate(spec.nodes)}

    for node_index, n in enumerate(spec.nodes):
        if n.type == "coding_agent" and n.writable and n.allowed_paths:
            raise _located_error(
                f"{source}:节点 {n.id} 的 writable: true 与 allowed_paths 不能同时使用;"
                "Claude CLI 的 --add-dir 不是只读边界",
                ("nodes", node_index, "allowed_paths"), _marks,
                fallback_paths=(("nodes", node_index),))
        for consume_index, c in enumerate(n.consumes):
            if c == "task":
                continue
            # 精确匹配 <节点id>.output / <节点id>.diff(coding_agent 的第二产物):
            # 前缀匹配会放过 node_a.output.output 这类笔误,把接线错误留到运行期才炸
            ok = False
            for suffix, producer_type, need_on_error in (
                    (".output", None, None),
                    (".diff", "coding_agent", None),
                    (".error", "llm", "branch")):
                if c.endswith(suffix):
                    producer = c[: -len(suffix)]
                    if producer in by_id:
                        pnode = by_id[producer]
                        if ((producer_type is None or pnode.type == producer_type)
                                and (need_on_error is None
                                     or pnode.on_error == need_on_error)):
                            ok = True
                    break
            if not ok:
                raise _located_error(
                    f"{source}:节点 {n.id} 消费 {c!r},但不存在能产出它的节点。"
                    f"consumes 只能引用 'task'、'<节点id>.output'、coding_agent "
                    f"节点的 '<节点id>.diff',或 on_error: branch 节点的 "
                    f"'<节点id>.error'(已知节点:{sorted(ids)})",
                    ("nodes", node_index, "consumes", consume_index), _marks,
                    fallback_paths=(("nodes", node_index, "consumes"),
                                    ("nodes", node_index)),
                )

    for edge_index, e in enumerate(spec.edges):
        if e.source not in by_id:
            raise _located_error(
                f"{source}:边 {e.source!r} → {e.target!r} 的 from 不是任何节点",
                ("edges", edge_index, "from"), _marks,
                fallback_paths=(("edges", edge_index),))
        if e.target != "END" and e.target not in by_id:
            raise _located_error(
                f"{source}:边 {e.source!r} → {e.target!r} 的 to 不是任何节点也不是 END",
                ("edges", edge_index, "to"), _marks,
                fallback_paths=(("edges", edge_index),))

    _check_edge_groups(spec, source=source, by_id=by_id,
                       node_indexes=node_indexes, marks=_marks)
    _check_on_error(spec, source=source, by_id=by_id,
                    node_indexes=node_indexes, marks=_marks)
    entry = _resolve_entry(spec, source=source, by_id=by_id, marks=_marks)
    _check_reachable(spec, entry, source=source, marks=_marks)
    _check_cycles(spec, source=source, by_id=by_id, marks=_marks)
    return entry


def _check_on_error(spec, *, source, by_id, node_indexes,
                    marks: _SourceMarks | None = None) -> None:
    """P3:on_error 与保留路由键 __failed__ 的结构校验(校验期强制)。

    - when: __failed__ 的边只出现在 on_error: branch 的节点上(保留键);
    - on_error: branch 必须有至少一条 __failed__ 出边——失败处理器要显式
      接线,不能等运行期现猜;
    - on_error: continue 不能带条件出边:软失败后节点没有输出,条件路由
      不可判定(NoRouteError 是治理错误,continue 吞不掉它)。
    """
    out: dict[str, list[EdgeSpec]] = {}
    for e in spec.edges:
        out.setdefault(e.source, []).append(e)
    for n in spec.nodes:
        edges = out.get(n.id, [])
        failed_edges = [e for e in edges if e.when == FAILED_EDGE_KEY]
        node_path = ("nodes", node_indexes[n.id])
        if failed_edges and n.on_error != "branch":
            raise _located_error(
                f"{source}:节点 {n.id} 有 when: {FAILED_EDGE_KEY} 的边,但它的"
                f" on_error 不是 branch。{FAILED_EDGE_KEY} 是保留路由键,只在"
                f" on_error: branch 的节点上合法",
                node_path + ("on_error",), marks, fallback_paths=(node_path,))
        if n.on_error == "branch" and not failed_edges:
            raise _located_error(
                f"{source}:节点 {n.id} 声明 on_error: branch,但没有一条 "
                f"when: {FAILED_EDGE_KEY} 的出边。失败处理器必须在校验期"
                f"显式接线,不能运行期现猜",
                node_path + ("on_error",), marks, fallback_paths=(node_path,))
        if n.on_error == "continue":
            conditional = [e for e in edges if e.when is not None]
            if conditional:
                raise _located_error(
                    f"{source}:节点 {n.id} 声明 on_error: continue,但它有条件"
                    f"出边(when)。软失败后节点没有输出,条件路由不可判定;"
                    f"要图继续就用无条件出边,要接失败处理器就用 branch",
                    node_path + ("on_error",), marks, fallback_paths=(node_path,))


def _check_edge_groups(spec, *, source, by_id, node_indexes,
                       marks: _SourceMarks | None = None) -> None:
    """同一来源的出边:无条件边(可多条=并行扇出)与条件边(全部带 when,值互不相同)
    不可混用——混用的话「这条边走不走」没有可判定的答案。

    例外(P3):保留键 when: __failed__ 是失败专用边,可与成功路径的任意
    边型共存(成功→无条件扇出或正常条件路由;软失败→保留键),但每个
    来源最多一条——路由表按键寻址,重复键没有可判定的目标。
    """
    out: dict[str, list[tuple[int, EdgeSpec]]] = {}
    for edge_index, e in enumerate(spec.edges):
        out.setdefault(e.source, []).append((edge_index, e))
    for src, indexed_edges in out.items():
        conditional = [(i, e) for i, e in indexed_edges
                       if e.when is not None and e.when != FAILED_EDGE_KEY]
        unconditional = [(i, e) for i, e in indexed_edges if e.when is None]
        failed = [(i, e) for i, e in indexed_edges
                  if e.when == FAILED_EDGE_KEY]
        if len(failed) > 1:
            edge_index = failed[1][0]
            raise _located_error(
                f"{source}:节点 {src} 有 {len(failed)} 条 when: {FAILED_EDGE_KEY}"
                f" 的边,最多一条(路由表按键寻址)",
                ("edges", edge_index, "when"), marks,
                fallback_paths=(("edges", edge_index),))
        if conditional and unconditional:
            edge_index = conditional[0][0]
            raise _located_error(
                f"{source}:节点 {src} 的出边混用了条件与无条件边。"
                f"要么只写无条件边(多条=并行扇出),要么全部带 when"
                f"(when: {FAILED_EDGE_KEY} 的失败专用边除外)",
                ("edges", edge_index, "when"), marks,
                fallback_paths=(("edges", edge_index),))
        seen_whens: set[str] = set()
        duplicate_index = None
        for edge_index, edge in conditional:
            if edge.when in seen_whens:
                duplicate_index = edge_index
                break
            seen_whens.add(edge.when)
        if duplicate_index is not None:
            whens = [e.when for _, e in conditional]
            raise _located_error(
                f"{source}:节点 {src} 的条件边 when 值重复:{sorted(whens)}",
                ("edges", duplicate_index, "when"), marks,
                fallback_paths=(("edges", duplicate_index),))
        node = by_id[src]
        if conditional:   # 正常条件边才要求路由字段;纯 __failed__ 不需要
            node_index = node_indexes[src]
            if node.required_fields is None:
                raise _located_error(
                    f"{source}:节点 {src} 有条件出边,必须声明 output_schema.required"
                    f"(路由字段 {node.route_field!r} 要在里面)",
                    ("nodes", node_index, "output_schema", "required"), marks,
                    fallback_paths=(("nodes", node_index),))
            if node.route_field not in node.required_fields:
                raise _located_error(
                    f"{source}:节点 {src} 的路由字段 {node.route_field!r} "
                    f"不在 output_schema.required {node.required_fields} 里。"
                    f"路由按这个字段的值查表,缺了它就没法路由",
                    ("nodes", node_index, "output_schema", "required"), marks,
                    fallback_paths=(("nodes", node_index, "route_field"),
                                    ("nodes", node_index)),
                )


def _resolve_entry(spec, *, source, by_id,
                   marks: _SourceMarks | None = None) -> str:
    """显式 entries(多入口)逐个校验;单 entry 兼容旧格式;
    都没有时推断——唯一根=入口;多根=多入口并行(M4 起合法);无根=全环,拒绝。"""
    if spec.entries:
        for index, e in enumerate(spec.entries):
            if e not in by_id:
                raise _located_error(f"{source}:entry {e!r} 不在节点清单里",
                                     ("entry", index), marks,
                                     fallback_paths=(("entry",),))
        return spec.entries[0]
    if spec.entry:
        if spec.entry not in by_id:
            raise _located_error(
                f"{source}:entry {spec.entry!r} 不在节点清单里",
                ("entry",), marks)
        return spec.entry
    incoming = {e.target for e in spec.edges if e.target != "END"}
    roots = [nid for nid in by_id if nid not in incoming]
    if not roots:
        raise _located_error(
            f"{source}:推断不出入口(每个节点都有入边,图里只有环)。"
            f"请显式写 entry: <节点id>",
            ("entry",), marks, use_mark=False,
        )
    return roots[0]   # 多根 = 多入口并行,合法;spec_from_yaml 会把全部根写进 entries


def _infer_roots(spec, by_id) -> list[str]:
    incoming = {e.target for e in spec.edges if e.target != "END"}
    return [nid for nid in by_id if nid not in incoming]


def _check_reachable(spec, entry, *, source,
                     marks: _SourceMarks | None = None) -> None:
    # 起点优先级与 all_entries() 的执行起点严格一致:
    # 显式 entries → 显式单值 entry → 推断根(多根=多入口)。
    # 显式 entry: left 的多根图里,right 从 left 不可达 → 拒收,
    # 提示用户显式选择入口——不静默多跑,也不静默丢弃分支。
    if spec.entries:
        starts = list(spec.entries)
    elif spec.entry:
        starts = [spec.entry]
    else:
        starts = _infer_roots(spec, {n.id for n in spec.nodes}) or [entry]
    seen = set()
    stack = list(starts)
    adj: dict[str, list[str]] = {}
    for e in spec.edges:
        if e.target != "END":
            adj.setdefault(e.source, []).append(e.target)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(adj.get(cur, []))
    unreachable = sorted(set(n.id for n in spec.nodes) - seen)
    if unreachable:
        # 多个节点/边共同导致，不把任意一个源码位置冒充根因。
        raise _located_error(
            f"{source}:存在从入口 {starts!r} 不可达的节点:{unreachable}。"
            f"不可达节点永远不会执行",
            ("nodes",), marks, use_mark=False,
        )


def _tarjan_sccs(nodes: list[str], adj: dict[str, list[str]]) -> list[list[str]]:
    """迭代版 Tarjan(不递归:幻觉 YAML 可能有很深的链,校验器自己不能先崩)。"""
    idx: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in idx:
            continue
        idx[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        call: list[tuple[str, iter]] = [(root, iter(adj.get(root, [])))]
        while call:
            node, it = call[-1]
            advanced = False
            for nxt in it:
                if nxt not in idx:
                    idx[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    call.append((nxt, iter(adj.get(nxt, []))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], idx[nxt])
            if advanced:
                continue
            call.pop()
            if call:
                parent = call[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == idx[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                sccs.append(comp)
    return sccs


def _check_cycles(spec, *, source, by_id,
                  marks: _SourceMarks | None = None) -> None:
    """死环 = 强连通分量里没有任何条件出口。有环必须显式设 max_iterations。

    注意判据是 SCC 级别的「存在 when 边通向环外或 END」,不是「环上节点
    不许有无条件边」——maker→judge 无条件 + judge 条件退出的修复环是合法的。
    """
    adj: dict[str, list[str]] = {}
    for e in spec.edges:
        if e.target != "END":
            adj.setdefault(e.source, []).append(e.target)

    cyclic_comps = [
        comp for comp in _tarjan_sccs(list(by_id), adj)
        if len(comp) > 1 or comp[0] in adj.get(comp[0], [])
    ]
    if not cyclic_comps:
        return
    if spec.guards.max_iterations is None:
        on_cycle = sorted(n for comp in cyclic_comps for n in comp)
        raise _located_error(
            f"{source}:图里有环(节点 {on_cycle}),但 guards.max_iterations "
            f"没有显式设置。不设上限的循环会烧钱到天荒地老",
            ("guards", "max_iterations"), marks, use_mark=False,
        )
    for comp in cyclic_comps:
        comp_set = set(comp)
        exits = [
            e for nid in comp for e in spec.outgoing(nid)
            if e.when is not None and (e.target == "END" or e.target not in comp_set)
        ]
        if not exits:
            # 整个 SCC 和多条边共同导致死环，只有相关路径，没有唯一源码位置。
            raise _located_error(
                f"{source}:环 {sorted(comp)} 没有条件出口——死环,永远退不出去。"
                f"环上至少要有一个带 when 的出边通向环外或 END",
                ("edges",), marks, use_mark=False,
            )
