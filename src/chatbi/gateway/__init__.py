"""模型网关：唯一外发入口（步骤 02）。

* :mod:`chatbi.gateway.policy` —— 出口契约、脱敏、请求重建、响应校验、本地审计；
* :mod:`chatbi.gateway.client` —— httpx 固定地址客户端、预算与假传输；
* :mod:`chatbi.gateway.server` —— 私有 Unix socket 服务与本地调用。

密钥只从环境变量 ``CHATBI_LLM_API_KEY`` 读取，任何模块都不会把它写入文件或日志。
"""

from chatbi.gateway.client import GatewayClient, GatewayResult  # noqa: F401

__all__ = ["GatewayClient", "GatewayResult"]
