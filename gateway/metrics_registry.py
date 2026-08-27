# -*- coding: utf-8 -*-
"""线程安全 Prometheus 文本格式指标注册表（零依赖，JKagent 收官）。

提供 Counter / Gauge / Histogram 三类指标与 render_prometheus()
标准文本格式输出（# HELP / # TYPE / _bucket/_sum/_count），供
GET /metrics 端点消费。设计要点：

- 零第三方依赖：不引入 prometheus_client，格式手写且逐行验证。
- 线程安全：每个指标家族一把锁覆盖单元格读写与渲染快照；注册表一把锁
  覆盖注册/查找/渲染枚举。agent executor 线程（agent 主线程 + 并行工具
  线程）与事件循环线程并发 observe 不丢样本。
- 标签：可选 labelnames；无标签时输出裸指标行，有标签时输出
  name{label="value",...}（按标签名顺序稳定排序）。
- 直方图：可配桶边界，默认 [0.05,0.1,0.25,0.5,1,2.5,5,10,30]（秒级，
  适配 turn 总耗时/首 delta 延迟量级）；桶计数为累积值
  （le="+Inf" 行与 _count 行等价于总样本数）。
- 渲染确定性：家族按名称排序、单元格按标签值元组排序，输出可 diff。
"""

import re
import threading

# 默认直方图桶边界（秒）。turn 总耗时与首 delta 延迟同量级（百毫秒~数十秒）。
DEFAULT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

_METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _escape_label_value(value) -> str:
    """Prometheus 文本格式标签值转义：反斜杠、换行、双引号。"""
    return (str(value).replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace('"', '\\"'))


def _escape_help(text) -> str:
    """HELP 行转义：反斜杠与换行。"""
    return str(text or "").replace("\\", "\\\\").replace("\n", "\\n")


def _format_number(value) -> str:
    """整数输出不带小数点；浮点整数值输出为整数形式（Prometheus 惯例）。"""
    if isinstance(value, int):
        return str(value)
    f = float(value)
    if f.is_integer():
        return str(int(f))
    return repr(f)


class _MetricFamily:
    """带标签指标家族基类：单锁覆盖单元格读写与渲染快照。"""

    kind = "untyped"

    def __init__(self, name: str, help_text: str = "", labelnames=()):
        if not _METRIC_NAME_RE.match(name):
            raise ValueError(f"非法指标名: {name!r}")
        labelnames = tuple(labelnames)
        for label_name in labelnames:
            if not _LABEL_NAME_RE.match(label_name):
                raise ValueError(f"非法标签名: {label_name!r}")
        self.name = name
        self.help_text = help_text or ""
        self.labelnames = labelnames
        self._lock = threading.Lock()
        # 单元格：标签值元组 -> 数值/累计状态；无标签时键为 ()
        self._cells = {}

    def _key(self, labels) -> tuple:
        """把调用方标签 dict 规范化为按 labelnames 顺序的元组（缺失补空串）。"""
        if not labels:
            labels = {}
        return tuple(str(labels.get(ln, "")) for ln in self.labelnames)

    def _labels_suffix(self, key: tuple) -> str:
        """标签序列化：无标签返回空串；有标签返回 {k="v",...}。"""
        if not self.labelnames:
            return ""
        inner = ",".join(
            f'{ln}="{_escape_label_value(v)}"'
            for ln, v in zip(self.labelnames, key))
        return "{" + inner + "}"

    def _snapshot_cells(self):
        """锁内拷贝并按标签值排序，供渲染使用（渲染期间不持锁）。"""
        with self._lock:
            return sorted(self._cells.items())

    def _zero_cell_lines(self):
        """无观测单元格时的零值样本行（与 prometheus_client 语义一致：
        无标签家族输出 0；有标签家族无观测则不输出，避免伪造标签维度）。"""
        if self.labelnames:
            return []
        if self.kind == "histogram":
            lines = []
            for le in [str(b) for b in self.buckets] + ["+Inf"]:
                lines.append(f'{self.name}_bucket{{le="{le}"}} 0')
            lines.append(f"{self.name}_sum 0")
            lines.append(f"{self.name}_count 0")
            return lines
        return [f"{self.name} 0"]


class Counter(_MetricFamily):
    """单调递增计数器（Prometheus counter；值非负）。"""

    kind = "counter"

    def inc(self, value=1, labels=None) -> None:
        if value < 0:
            raise ValueError(f"counter {self.name} 不能增加负值: {value}")
        key = self._key(labels)
        with self._lock:
            self._cells[key] = self._cells.get(key, 0.0) + value

    def render_lines(self):
        cells = self._snapshot_cells()
        if not cells:
            return self._zero_cell_lines()
        lines = []
        for key, value in cells:
            lines.append(f"{self.name}{self._labels_suffix(key)} "
                         f"{_format_number(value)}")
        return lines


class Gauge(_MetricFamily):
    """可增可减的当前值指标。"""

    kind = "gauge"

    def set(self, value, labels=None) -> None:
        key = self._key(labels)
        with self._lock:
            self._cells[key] = float(value)

    def inc(self, value=1, labels=None) -> None:
        key = self._key(labels)
        with self._lock:
            self._cells[key] = self._cells.get(key, 0.0) + value

    def dec(self, value=1, labels=None) -> None:
        key = self._key(labels)
        with self._lock:
            self._cells[key] = self._cells.get(key, 0.0) - value

    def render_lines(self):
        cells = self._snapshot_cells()
        if not cells:
            return self._zero_cell_lines()
        lines = []
        for key, value in cells:
            lines.append(f"{self.name}{self._labels_suffix(key)} "
                         f"{_format_number(value)}")
        return lines


class Histogram(_MetricFamily):
    """观测值直方图：累积桶计数 + sum + count（Prometheus histogram）。"""

    kind = "histogram"

    def __init__(self, name: str, help_text: str = "",
                 buckets=None, labelnames=()):
        super().__init__(name, help_text, labelnames=labelnames)
        if buckets is None:
            buckets = DEFAULT_BUCKETS
        buckets = sorted(float(b) for b in buckets)
        if not buckets or any(b < 0 for b in buckets):
            raise ValueError(f"histogram {name} 桶边界必须为正数列表")
        self.buckets = tuple(buckets)

    def observe(self, value, labels=None) -> None:
        """线程安全累加一次观测。

        桶计数为累积值：value <= buckets[i] 的桶 i 及更大桶全部 +1；
        le="+Inf" 桶（_cells 末位）恒等于总样本数。
        """
        value = float(value)
        key = self._key(labels)
        bucket_count = len(self.buckets)
        with self._lock:
            cell = self._cells.get(key)
            if cell is None:
                cell = self._cells[key] = {
                    # 累积桶计数：index 0..bucket_count-1 对应各桶，
                    # index bucket_count 为 +Inf 桶
                    "cum": [0.0] * (bucket_count + 1),
                    "sum": 0.0,
                    "count": 0.0,
                }
            idx = 0
            for upper in self.buckets:
                if value <= upper:
                    break
                idx += 1
            # value 落在第一个满足 value<=upper 的桶 idx（含 +Inf 兜底）
            for i in range(idx, bucket_count + 1):
                cell["cum"][i] += 1
            cell["sum"] += value
            cell["count"] += 1

    def render_lines(self):
        cells = self._snapshot_cells()
        if not cells:
            return self._zero_cell_lines()
        lines = []
        le_values = [str(b) for b in self.buckets] + ["+Inf"]
        for key, cell in cells:
            base = self.name
            for i, le in enumerate(le_values):
                label_parts = [f'le="{le}"']
                if self.labelnames:
                    label_parts.extend(
                        f'{ln}="{_escape_label_value(v)}"'
                        for ln, v in zip(self.labelnames, key))
                lines.append(
                    f"{base}_bucket{{{','.join(label_parts)}}} "
                    f"{_format_number(cell['cum'][i])}")
            lines.append(f"{base}_sum{self._labels_suffix(key)} "
                         f"{_format_number(cell['sum'])}")
            lines.append(f"{base}_count{self._labels_suffix(key)} "
                         f"{_format_number(cell['count'])}")
        return lines


class MetricsRegistry:
    """指标注册表：按名称注册家族，幂等复用；render 输出标准文本格式。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._families = {}

    def _get_or_create(self, name, help_text, labelnames, cls, **kwargs):
        labelnames = tuple(labelnames)
        with self._lock:
            family = self._families.get(name)
            if family is None:
                family = cls(name, help_text, labelnames=labelnames, **kwargs)
                self._families[name] = family
                return family
            if not isinstance(family, cls):
                raise ValueError(
                    f"指标 {name} 已注册为不同类型的家族 "
                    f"({type(family).__name__} vs {cls.__name__})")
            if family.labelnames != labelnames:
                raise ValueError(f"指标 {name} 标签集不一致: "
                                 f"{family.labelnames} vs {labelnames}")
            # 幂等：help 以首次注册为准，后续注册沿用已有家族
            return family

    def counter(self, name: str, help_text: str = "", labelnames=()) -> Counter:
        return self._get_or_create(name, help_text, labelnames, Counter)

    def gauge(self, name: str, help_text: str = "", labelnames=()) -> Gauge:
        return self._get_or_create(name, help_text, labelnames, Gauge)

    def histogram(self, name: str, help_text: str = "",
                  buckets=None, labelnames=()) -> Histogram:
        return self._get_or_create(
            name, help_text, labelnames, Histogram, buckets=buckets)

    def render_prometheus(self) -> str:
        """渲染标准 Prometheus 文本格式（UTF-8，尾部换行）。

        结构：每家族 # HELP + # TYPE + 样本行；家族按名称排序、样本行
        按标签值排序，输出确定性可 diff。
        """
        with self._lock:
            families = sorted(self._families.values(), key=lambda f: f.name)
        lines = []
        for family in families:
            lines.append(f"# HELP {family.name} {_escape_help(family.help_text)}")
            lines.append(f"# TYPE {family.name} {family.kind}")
            lines.extend(family.render_lines())
        return "\n".join(lines) + "\n" if lines else ""

