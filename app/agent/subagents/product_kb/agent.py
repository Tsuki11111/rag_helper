"""产品知识库子智能体组合根：接收已编译的 RAG 图。"""
from deepagents import CompiledSubAgent
from langchain_core.runnables import RunnableLambda

from app.core.load_prompt import load_prompt


def build_product_kb_subagent(rag_task) -> CompiledSubAgent:
    return CompiledSubAgent(
        name="product-kb",
        description=load_prompt("product_kb_description").strip(),
        runnable=RunnableLambda(rag_task),
    )
