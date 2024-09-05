from log_config import logger

import httpx
import secrets
from contextlib import asynccontextmanager

from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from models import RequestModel, ImageGenerationRequest
from request import get_payload
from response import fetch_response, fetch_response_stream
from utils import error_handling_wrapper, post_all_models, load_config

from typing import List, Dict, Union
from urllib.parse import urlparse

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时的代码
    import os
    TIMEOUT = float(os.getenv("TIMEOUT", 20))
    timeout = httpx.Timeout(connect=15.0, read=TIMEOUT, write=30.0, pool=30.0)
    default_headers = {
        "User-Agent": "curl/7.68.0",  # 模拟 curl 的 User-Agent
        "Accept": "*/*",  # curl 的默认 Accept 头
    }
    app.state.client = httpx.AsyncClient(
        timeout=timeout,
        headers=default_headers,
        http2=True,  # 禁用 HTTP/2
        verify=True,  # 保持 SSL 验证（如需禁用，设为 False，但不建议）
        follow_redirects=True,  # 自动跟随重定向
    )
    # app.state.client = httpx.AsyncClient(timeout=timeout)
    app.state.config, app.state.api_keys_db, app.state.api_list = await load_config(app)
    yield
    # 关闭时的代码
    await app.state.client.aclose()

app = FastAPI(lifespan=lifespan)

import asyncio
from time import time
from collections import defaultdict
from starlette.middleware.base import BaseHTTPMiddleware
from datetime import datetime
from datetime import timedelta
import json
import aiofiles

class StatsMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, exclude_paths=None, save_interval=3600, filename="stats.json"):
        super().__init__(app)
        self.request_counts = defaultdict(int)
        self.request_times = defaultdict(float)
        self.ip_counts = defaultdict(lambda: defaultdict(int))
        self.request_arrivals = defaultdict(list)
        self.lock = asyncio.Lock()
        self.exclude_paths = set(exclude_paths or [])
        self.save_interval = save_interval
        self.filename = filename
        self.last_save_time = time()

        # 启动定期保存和清理任务
        asyncio.create_task(self.periodic_save_and_cleanup())

    async def dispatch(self, request: Request, call_next):
        arrival_time = datetime.now()
        start_time = time()
        response = await call_next(request)
        process_time = time() - start_time

        endpoint = f"{request.method} {request.url.path}"
        client_ip = request.client.host

        if request.url.path not in self.exclude_paths:
            async with self.lock:
                self.request_counts[endpoint] += 1
                self.request_times[endpoint] += process_time
                self.ip_counts[endpoint][client_ip] += 1
                self.request_arrivals[endpoint].append(arrival_time)

        return response

    async def periodic_save_and_cleanup(self):
        while True:
            await asyncio.sleep(self.save_interval)
            await self.save_stats()
            await self.cleanup_old_data()

    async def save_stats(self):
        current_time = time()
        if current_time - self.last_save_time < self.save_interval:
            return

        async with self.lock:
            stats = {
                "request_counts": dict(self.request_counts),
                "request_times": dict(self.request_times),
                "ip_counts": {k: dict(v) for k, v in self.ip_counts.items()},
                "request_arrivals": {k: [t.isoformat() for t in v] for k, v in self.request_arrivals.items()}
            }

        filename = self.filename
        async with aiofiles.open(filename, mode='w') as f:
            await f.write(json.dumps(stats, indent=2))

        self.last_save_time = current_time
        # print(f"Stats saved to {filename}")

    async def cleanup_old_data(self):
        # cutoff_time = datetime.now() - timedelta(seconds=30)
        cutoff_time = datetime.now() - timedelta(hours=24)
        async with self.lock:
            for endpoint in list(self.request_arrivals.keys()):
                self.request_arrivals[endpoint] = [
                    t for t in self.request_arrivals[endpoint] if t > cutoff_time
                ]
                if not self.request_arrivals[endpoint]:
                    del self.request_arrivals[endpoint]
                    self.request_counts.pop(endpoint, None)
                    self.request_times.pop(endpoint, None)
                    self.ip_counts.pop(endpoint, None)

    async def cleanup(self):
        await self.save_stats()

# 配置 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有 HTTP 方法
    allow_headers=["*"],  # 允许所有头部字段
)

app.add_middleware(StatsMiddleware, exclude_paths=["/stats", "/generate-api-key"])

async def process_request(request: Union[RequestModel, ImageGenerationRequest], provider: Dict, endpoint=None):
    url = provider['base_url']
    parsed_url = urlparse(url)
    # print(parsed_url)
    engine = None
    if parsed_url.netloc == 'generativelanguage.googleapis.com':
        engine = "gemini"
    elif parsed_url.netloc == 'aiplatform.googleapis.com':
        engine = "vertex"
    elif parsed_url.netloc == 'api.anthropic.com' or parsed_url.path.endswith("v1/messages"):
        engine = "claude"
    elif parsed_url.netloc == 'openrouter.ai':
        engine = "openrouter"
    else:
        engine = "gpt"

    if "claude" not in provider['model'][request.model] \
    and "gpt" not in provider['model'][request.model] \
    and "gemini" not in provider['model'][request.model]:
        engine = "openrouter"

    if endpoint == "/uni/v1/images/generations":
        engine = "dalle"
        request.stream = False

    if provider.get("engine"):
        engine = provider["engine"]

    logger.info(f"provider: {provider['provider']:<10} model: {request.model:<10} engine: {engine}")

    url, headers, payload = await get_payload(request, engine, provider)

    # request_info = {
    #     "url": url,
    #     "headers": headers,
    #     "payload": payload
    # }
    # import json
    # logger.info(f"Request details: {json.dumps(request_info, indent=4, ensure_ascii=False)}")

    if request.stream:
        model = provider['model'][request.model]
        generator = fetch_response_stream(app.state.client, url, headers, payload, engine, model)
        wrapped_generator = await error_handling_wrapper(generator, status_code=500)
        return StreamingResponse(wrapped_generator, media_type="text/event-stream")
    else:
        return await anext(fetch_response(app.state.client, url, headers, payload))

import asyncio
class ModelRequestHandler:
    def __init__(self):
        self.last_provider_index = -1

    def get_matching_providers(self, model_name, token):
        config = app.state.config
        # api_keys_db = app.state.api_keys_db
        api_list = app.state.api_list

        api_index = api_list.index(token)
        provider_rules = []

        for model in config['api_keys'][api_index]['model']:
            if "/" in model:
                provider_name = model.split("/")[0]
                model = model.split("/")[1]
                models_list = []
                for provider in config['providers']:
                    if provider['provider'] == provider_name:
                        models_list.extend(list(provider['model'].keys()))
                # print("models_list", models_list)
                # print("model_name", model_name)
                # print("model", model)
                if (model and model_name in models_list) or (model == "*" and model_name in models_list):
                    provider_rules.append(provider_name)
            else:
                for provider in config['providers']:
                    if model in provider['model'].keys():
                        provider_rules.append(provider['provider'] + "/" + model)

        provider_list = []
        # print("provider_rules", provider_rules)
        for item in provider_rules:
            for provider in config['providers']:
                # print("provider", provider, provider['provider'] == item, item)
                if "/" in item:
                    if provider['provider'] == item.split("/")[0]:
                        if model_name in provider['model'].keys() and item.split("/")[1] == model_name:
                            provider_list.append(provider)
                elif provider['provider'] == item:
                    if model_name in provider['model'].keys():
                        provider_list.append(provider)
                else:
                    pass

                # if provider['provider'] == item:
                #     if "/" in item:
                #         if item.split("/")[1] == model_name:
                #             provider_list.append(provider)
                #     else:
                #         if model_name in provider['model'].keys():
                #             provider_list.append(provider)

        # import json
        # for provider in provider_list:
        #     print(json.dumps(provider, indent=4, ensure_ascii=False))
        return provider_list

    async def request_model(self, request: Union[RequestModel, ImageGenerationRequest], token: str, endpoint=None):
        config = app.state.config
        # api_keys_db = app.state.api_keys_db
        api_list = app.state.api_list

        model_name = request.model
        matching_providers = self.get_matching_providers(model_name, token)
        # import json
        # print("matching_providers", json.dumps(matching_providers, indent=4, ensure_ascii=False))
        if not matching_providers:
            raise HTTPException(status_code=404, detail="No matching model found")

        # 检查是否启用轮询
        api_index = api_list.index(token)
        use_round_robin = True
        auto_retry = True
        if config['api_keys'][api_index].get("preferences"):
            if config['api_keys'][api_index]["preferences"].get("USE_ROUND_ROBIN") == False:
                use_round_robin = False
            if config['api_keys'][api_index]["preferences"].get("AUTO_RETRY") == False:
                auto_retry = False

        return await self.try_all_providers(request, matching_providers, use_round_robin, auto_retry, endpoint)

    async def try_all_providers(self, request: Union[RequestModel, ImageGenerationRequest], providers: List[Dict], use_round_robin: bool, auto_retry: bool, endpoint: str = None):
        num_providers = len(providers)
        start_index = self.last_provider_index + 1 if use_round_robin else 0

        for i in range(num_providers + 1):
            self.last_provider_index = (start_index + i) % num_providers
            provider = providers[self.last_provider_index]
            try:
                response = await process_request(request, provider, endpoint)
                return response
            except (Exception, HTTPException, asyncio.CancelledError, httpx.ReadError) as e:
                logger.error(f"Error with provider {provider['provider']}: {str(e)}")
                if auto_retry:
                    continue
                else:
                    raise HTTPException(status_code=500, detail="Error: Current provider response failed!")


        raise HTTPException(status_code=500, detail=f"All providers failed: {request.model}")

model_handler = ModelRequestHandler()

# 安全性依赖
security = HTTPBearer()

def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    api_list = app.state.api_list
    token = credentials.credentials
    if token not in api_list:
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")
    return token

def verify_admin_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    api_list = app.state.api_list
    token = credentials.credentials
    if token not in api_list:
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")
    for api_key in app.state.api_keys_db:
        if api_key['api'] == token:
            if api_key.get('role') != "admin":
                raise HTTPException(status_code=403, detail="Permission denied")
    return token

@app.post("/uni/v1/chat/completions")
async def request_model(request: Union[RequestModel, ImageGenerationRequest], token: str = Depends(verify_api_key)):
    return await model_handler.request_model(request, token)

@app.options("/uni/v1/chat/completions")
async def options_handler():
    return JSONResponse(status_code=200, content={"detail": "OPTIONS allowed"})

@app.get("/uni/v1/models")
async def list_models(token: str = Depends(verify_api_key)):
    models = post_all_models(token, app.state.config, app.state.api_list)
    return JSONResponse(content={
        "object": "list",
        "data": models
    })

@app.post("/uni/v1/images/generations")
async def images_generations(
    request: ImageGenerationRequest,
    token: str = Depends(verify_api_key)
):
    return await model_handler.request_model(request, token, endpoint="/uni/v1/images/generations")

@app.get("/generate-api-key")
def generate_api_key():
    api_key = "sk-" + secrets.token_urlsafe(36)
    return JSONResponse(content={"api_key": api_key})

@app.get("/stats")
async def get_stats(request: Request, token: str = Depends(verify_admin_api_key)):
    middleware = app.middleware_stack.app
    if isinstance(middleware, StatsMiddleware):
        async with middleware.lock:
            stats = {
                "request_counts": dict(middleware.request_counts),
                "request_times": dict(middleware.request_times),
                "ip_counts": {k: dict(v) for k, v in middleware.ip_counts.items()},
                "request_arrivals": {k: [t.isoformat() for t in v] for k, v in middleware.request_arrivals.items()}
            }
        return JSONResponse(content=stats)
    return {"error": "StatsMiddleware not found"}

# async def on_fetch(request, env):
#     import asgi
#     return await asgi.fetch(app, request, env)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(
        "__main__:app",
        host="0.0.0.0",
        port=7860,
        reload=True,
        ws="none",
        # log_level="warning"
    )