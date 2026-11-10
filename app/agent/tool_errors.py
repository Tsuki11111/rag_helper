"""工具调用的结构化失败类型。

Java 内部接口的错误原先只靠中文文案前缀判断（`"操作未完成"` / `"平台服务暂时不可用"`
等），文案一改，表单回传或批量汇总就会把失败当成功。这里把「失败」变成类型：

- `ToolError` 是 `str` 的子类，保留原有可读文案，工具/模型侧无需改协议；
- `.kind` 给出稳定分类（unauthorized/transient/business/invalid_response/internal），
  调用方只认类型，不再匹配中文。

失败文案仍由 `its_mygo_client.call` 生成；`is_failure_text` 仅用于兼容历史上已经
被摊平成字符串的返回值（例如 `order_actions` 的草稿函数）。
"""

FAILURE_PREFIXES = (
    "预检未通过",
    "预检失败",
    "操作未完成",
    "平台服务暂时不可用",
    "平台工具凭证",
    "平台内部错误",
    "平台返回格式异常",
)


class ToolError(str):
    """带稳定分类的工具失败文案（同时保持字符串兼容）。"""

    def __new__(cls, kind: str, message: str):
        obj = str.__new__(cls, message)
        obj.kind = kind
        return obj


def failure_kind(value) -> str | None:
    """返回失败分类；成功或无法判断时返回 None。"""
    if isinstance(value, ToolError):
        return value.kind
    if not isinstance(value, str) or not value:
        return None
    if "没有提交成功" in value:
        return "transient"
    for prefix in FAILURE_PREFIXES:
        if value.startswith(prefix):
            return "business"
    return None


def is_failure_text(value) -> bool:
    """字符串层面的失败判断（兼容尚未携带 ToolError 的旧返回值）。"""
    return failure_kind(value) is not None
