# 环境配置与依赖导入
import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.exceptions import LangChainException
from typing import Optional

# 项目内部依赖
from app.conf.budget_config import budget_config
from app.conf.lm_config import lm_config
from app.core.logger import logger
from app.core.retry import retry_call
from app.core.usage_tracker import TokenUsageCallback

# 全局缓存：键为(模型名, JSON输出模式, 端点, 是否带重试)元组，值为ChatOpenAI实例
# 作用：避免重复初始化客户端，提升性能，统一实例管理
_llm_client_cache = {}


class _RetryingChatOpenAI(ChatOpenAI):
    """
    暂时性故障（超时 / 连接失败 / 429 / 5xx）按**项目统一策略**重试的 ChatOpenAI

    给主 agent 用。它的模型调用走 langgraph 的模型节点（`model.invoke` → `_generate`，
    非流式），外层没有节点级的 `retry_call` 包着 —— 实测两次「连不上 api.deepseek.com」
    直接让整轮失败（2026-10-14，网络抖动）。在这里补上，与 RAG 节点同一套
    `error_policy` / `budget_config`，每次重试同样留痕（见 app/core/retry.py）。

    `_stream` 刻意不动：一旦开始往外吐字就不能重试（会重复推字），
    与 retry 模块的约定一致；主 agent 这条路本来就是非流式调用。
    """

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return retry_call(
            lambda: ChatOpenAI._generate(self, messages, stop=stop,
                                         run_manager=run_manager, **kwargs),
            what=f"主 agent 模型调用（{self.model_name}）",
        )


def get_llm_client(model: Optional[str] = None, json_mode: bool = False,
                   base_url: Optional[str] = None, api_key: Optional[str] = None,
                   retry_transient: bool = False) -> ChatOpenAI:
    """
    获取带全局缓存的LangChain ChatOpenAI客户端实例
    适配OpenAI/千问/即梦AI等**OpenAI兼容API**，支持自定义模型和JSON标准化输出
    核心特性：缓存机制+配置统一加载+异常精准捕获+国产模型参数适配

    :param model: 模型名称，优先级：传入参数 > 配置文件lm_config.llm_model > 内置默认qwen3-32b
    :param json_mode: 是否开启JSON输出模式，开启后返回标准json_object格式（适配结构化数据解析）
    :param base_url: 覆盖默认端点；None 用 lm_config.base_url。
        主 agent 走 DeepSeek 官方 API 时传它（与百炼端点不同，见 lm_config）
    :param api_key: 覆盖默认密钥；None 用 lm_config.api_key
    :param retry_transient: 暂时性故障按项目策略重试（`_RetryingChatOpenAI`）。
        **默认关**：RAG 各节点自己在调用点用 `retry_call`/`invoke_with_retry` 包了，
        再叠一层会「重试套重试」；只有主 agent 的图内调用需要它
    :return: 初始化完成的ChatOpenAI实例（优先从全局缓存获取，未命中则新建并缓存）
    :raise ValueError: 缺失API密钥/基础地址等核心配置
    :raise Exception: 模型初始化失败（LangChain封装层异常）
    """
    # 1. 确定目标模型（优先级递减，保证模型名非空）；端点/密钥同理可被调用方覆盖
    target_model = model or lm_config.llm_model or "qwen-plus"
    target_base = base_url or lm_config.base_url
    target_key = api_key or lm_config.api_key
    # 缓存键：模型名+JSON模式+端点+重试开关，唯一标识不同配置的客户端
    # （端点进键：同一模型在百炼与官方直连是两条通道，不能共用实例）
    cache_key = (target_model, json_mode, target_base, retry_transient)

    # 2. 缓存命中：直接返回已初始化的实例，避免重复创建
    if cache_key in _llm_client_cache:
        logger.debug(f"[LLM客户端] 缓存命中，直接返回实例：模型={target_model}，JSON模式={json_mode}")
        return _llm_client_cache[cache_key]

    # 3. 核心配置校验：拦截缺失的API关键配置，提前抛出明确异常
    if not target_key:
        raise ValueError("[LLM客户端] 配置缺失：请在.env中配置OPENAI_API_KEY（大模型API密钥）")
    if not target_base:
        raise ValueError("[LLM客户端] 配置缺失：请在.env中配置OPENAI_API_BASE（API接口基础地址）")
    logger.info(f"[LLM客户端] 开始初始化新实例：模型={target_model}，JSON模式={json_mode}")

    # 4. 配置参数组装：区分「国产模型私有参数」和「OpenAI通用参数」
    # extra_body：千问专属私有参数（enable_thinking，关闭思考链输出）——
    # **只对 qwen 系透传**：第三方端点（如 DeepSeek 官方 API）不认这个字段，带上可能直接报错
    extra_body = {"enable_thinking": False} if target_model.startswith("qwen") else {}
    # model_kwargs：OpenAI通用参数，所有兼容API均支持
    model_kwargs = {}
    if json_mode:
        # 开启JSON标准输出模式，强制模型返回可解析的json_object
        model_kwargs["response_format"] = {"type": "json_object"}
        logger.debug(f"[LLM客户端] 已开启JSON输出模式，模型将返回标准JSON结构")

    # 5. 客户端初始化：捕获LangChain封装层异常，抛出更友好的提示
    client_cls = _RetryingChatOpenAI if retry_transient else ChatOpenAI
    try:
        llm_client = client_cls(
            model=target_model,  # 目标模型名
            temperature=lm_config.llm_temperature or 0.1,  # 低温度保证输出确定性（0~1）
            api_key=target_key,  # API密钥
            base_url=target_base,  # API基础地址（适配国产模型代理地址）
            extra_body=extra_body,  # 国产模型私有参数透传
            model_kwargs=model_kwargs,  # OpenAI通用参数
            # 每次调用都记一笔账（模型/tokens/延迟/成本），见 app/core/usage_tracker.py。
            # 挂在客户端上而不是各调用点：16 个节点里 8 处调用，逐个传 handler 必漏
            callbacks=[TokenUsageCallback()],
            # 流式也要用量。langchain-openai 只在 base_url 是 OpenAI 官方地址时才默认开启，
            # 本项目指向 DashScope，不显式打开的话最贵的那次调用（流式生成答案）会漏记
            stream_usage=True,
            # 超时：不设的话走 openai SDK 的隐式 600 秒，一个卡住的调用会让整轮问答一直挂着
            timeout=budget_config.llm_timeout,
            # max_retries=0 是刻意的：重试策略要等错误分布数据（README 要求），
            # 现在别让 SDK 偷偷重试 —— 那会让「超时」这件事看起来时好时坏
            max_retries=0,
        )
    except LangChainException as e:
        raise Exception(f"[LLM客户端] 模型【{target_model}】初始化失败（LangChain层）：{str(e)}") from e

    # 6. 新实例存入全局缓存，供后续调用复用
    _llm_client_cache[cache_key] = llm_client
    logger.info(f"[LLM客户端] 实例初始化成功并缓存：模型={target_model}，JSON模式={json_mode}")

    return llm_client


# ---------------------------
# 自测（regression 统一入口调用）
# ---------------------------
class _FakeTimeout(Exception):
    """名字里带 timeout —— error_policy 会归成 RETRYABLE（自测用，**必须模块级**：
    loguru sink 开了 enqueue，函数内定义的类在个别路径下 pickle 不了，见 retry.py 同款注释）"""


def _check_retry_wired() -> list:
    """
    离线自测：主 agent 的模型调用接上了「暂时性故障重试」

    由来（2026-10-14，qwen→DeepSeek 切换当天）：实测两次「连不上 api.deepseek.com」
    直接让整轮失败 —— 主 agent 的调用走 langgraph 的模型节点，外层没有节点级
    `retry_call` 包着（M4-T1 列过这个目标，当时未落地）。守两件事：

    1. 工厂开关：`retry_transient=True` 给出带重试的客户端、默认不给（RAG 节点在
       调用点已自己包了重试，叠一层会「重试套重试」）
    2. 行为：遇到可重试故障（超时/连接失败）真的重试一次 —— 父类 `_generate` 打桩、
       `time.sleep` 换掉，不碰网络、不真等

    :return: 问题描述列表，空表示通过
    """
    import app.core.retry as retry_module
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    problems = []
    # 显式给 dummy 密钥/端点：本用例不碰 .env、不联网（CI 干净环境也能跑）
    plain = get_llm_client("qwen-plus", retry_transient=False,
                           base_url="http://ci.invalid", api_key="ci-dummy")
    retrying = get_llm_client("qwen-plus", retry_transient=True,
                              base_url="http://ci.invalid", api_key="ci-dummy")
    if type(plain) is not ChatOpenAI or not isinstance(retrying, _RetryingChatOpenAI):
        problems.append(f"工厂开关不对：默认 {type(plain).__name__}、"
                        f"开重试 {type(retrying).__name__}")
        return problems

    calls = {"n": 0}
    real_generate = ChatOpenAI._generate
    real_sleep = retry_module.time.sleep

    def fake_generate(self, messages, stop=None, run_manager=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _FakeTimeout("connect timed out")
        return ChatResult(generations=[ChatGeneration(message=AIMessage("ok"))])

    try:
        ChatOpenAI._generate = fake_generate
        retry_module.time.sleep = lambda s: None
        result = retrying._generate([HumanMessage(content="自测")])
        if calls["n"] != 2:
            problems.append(f"可重试故障没有重试：只调了 {calls['n']} 次（应为 2）")
        if result.generations[0].message.content != "ok":
            problems.append("重试后拿到的结果不对")
    except Exception as e:
        problems.append(f"重试路径抛异常：{type(e).__name__}: {e}")
    finally:
        ChatOpenAI._generate = real_generate
        retry_module.time.sleep = real_sleep
    return problems


# 测试示例：验证客户端创建、缓存机制及日志输出
if __name__ == "__main__":
    logger.info("===== 开始执行LLM客户端工具测试 =====")
    try:
        # 测试1：默认配置（默认模型+普通模式）
        client1 = get_llm_client()
        logger.info("✅ 测试1通过：默认配置客户端创建成功")

        # 测试2：指定多模态模型（qwen-vl-plus）+ 普通模式
        client2 = get_llm_client(model="qwen-vl-plus")
        logger.info("✅ 测试2通过：指定多模态模型客户端创建成功")

        # 测试3：同一模型+模式，验证缓存命中
        client3 = get_llm_client(model="qwen-vl-plus")
        logger.info(f"✅ 测试3通过：缓存机制验证成功，client2与client3为同一实例：{client2 is client3}")

        # 测试4：开启JSON输出模式
        client4 = get_llm_client(model="qwen-plus", json_mode=True)
        logger.info("✅ 测试4通过：JSON输出模式客户端创建成功")

    except Exception as e:
        logger.error(f"❌ LLM客户端工具测试失败：{str(e)}", exc_info=True)
    finally:
        logger.info("===== LLM客户端工具测试结束 =====")