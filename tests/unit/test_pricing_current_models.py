"""现行模型名也要能算出成本，不能因为计价表缺失返回 None。"""
from app.conf.pricing_config import estimate_cost


def test_current_models_have_price():
    # 不读 embedding/lm 配置：CI 没有 .env，模型字段可能为空；
    # 这里校验的是计价表必须覆盖代码里使用的现行型号名。
    for model in ("qwen3.7-text-embedding", "qwen3.7-text-rerank", "qwen3-vl-plus"):
        assert estimate_cost(model, prompt_tokens=1000) is not None, model
