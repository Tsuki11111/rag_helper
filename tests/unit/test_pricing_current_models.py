"""现行模型名也要能算出成本，不能因为计价表缺失返回 None。"""
from app.conf.embedding_config import embedding_config
from app.conf.lm_config import lm_config
from app.conf.pricing_config import estimate_cost
from app.conf.reranker_config import reranker_config


def test_current_models_have_price():
    for model in (embedding_config.model, reranker_config.model, lm_config.lv_model):
        assert model, "模型名不应为空"
        assert estimate_cost(model, prompt_tokens=1000) is not None, model
