"""
LLM模型配置检查和测试脚本
验证LLM模型是否正常设置并且能够正常使用
"""
import os
import sys

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def check_environment_variables():
    """
    检查环境变量配置
    """
    print("\n" + "=" * 60)
    print("检查环境变量配置")
    print("=" * 60)
    
    env_vars = {
        "OPENAI_API_KEY": "OpenAI API密钥",
        "OPENAI_BASE_URL": "OpenAI API地址",
        "DASHSCOPE_API_KEY": "阿里云千问API密钥",
        "DEEPSEEK_API_KEY": "DeepSeek API密钥"
    }
    
    results = {}
    for var, desc in env_vars.items():
        value = os.getenv(var)
        if value:
            # 隐藏部分密钥内容
            masked = value[:8] + "****" + value[-4:] if len(value) > 12 else "****"
            results[var] = {"status": "已设置", "value": masked, "desc": desc}
            print(f"✅ {desc} ({var}): {masked}")
        else:
            results[var] = {"status": "未设置", "value": None, "desc": desc}
            print(f"❌ {desc} ({var}): 未设置")
    
    return results


def check_config_files():
    """
    检查配置文件
    """
    print("\n" + "=" * 60)
    print("检查配置文件")
    print("=" * 60)
    
    import yaml
    
    config_files = [
        "config/system_config.yaml",
        "config/app_config.yaml",
        "config/production.yaml"
    ]
    
    results = {}
    
    for config_file in config_files:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), config_file)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    content = yaml.safe_load(f)
                results[config_file] = {"status": "存在", "content": content}
                print(f"✅ {config_file}: 存在")
            except Exception as e:
                results[config_file] = {"status": "读取失败", "error": str(e)}
                print(f"⚠️ {config_file}: 读取失败 - {e}")
        else:
            results[config_file] = {"status": "不存在"}
            print(f"❌ {config_file}: 不存在")
    
    return results


def check_llm_service():
    """
    检查LLM服务
    """
    print("\n" + "=" * 60)
    print("检查LLM服务")
    print("=" * 60)
    
    results = {}
    
    # 检查OpenAI库
    try:
        from openai import OpenAI
        results["openai_library"] = {"status": "已安装"}
        print("✅ openai库: 已安装")
    except ImportError:
        results["openai_library"] = {"status": "未安装"}
        print("❌ openai库: 未安装")
    
    # 检查requests库
    try:
        import requests
        results["requests_library"] = {"status": "已安装"}
        print("✅ requests库: 已安装")
    except ImportError:
        results["requests_library"] = {"status": "未安装"}
        print("❌ requests库: 未安装")
    
    return results


def test_openai_provider():
    """
    测试OpenAI Provider
    """
    print("\n" + "=" * 60)
    print("测试OpenAI Provider")
    print("=" * 60)
    
    results = {}
    
    try:
        from src.common.llm_service import OpenAIProvider
        
        # 检查API密钥
        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        
        if not api_key:
            print("⚠️ OPENAI_API_KEY未设置，跳过测试")
            results["status"] = "skipped"
            results["reason"] = "API密钥未设置"
            return results
        
        # 创建Provider
        provider = OpenAIProvider(api_key=api_key, base_url=base_url)
        
        print(f"API地址: {base_url}")
        print(f"客户端状态: {'已初始化' if provider.client else '未初始化'}")
        
        if provider.client:
            # 测试简单对话
            print("\n测试对话功能...")
            try:
                response = provider.chat(
                    prompt="你好，请简单回复一下。",
                    system_prompt="你是一个友好的助手。",
                    max_tokens=50
                )
                print(f"响应: {response[:100]}...")
                results["chat_test"] = {"status": "成功", "response": response[:100]}
                print("✅ 对话功能: 正常")
            except Exception as e:
                results["chat_test"] = {"status": "失败", "error": str(e)}
                print(f"❌ 对话功能: 失败 - {e}")
        else:
            print("⚠️ OpenAI客户端未初始化（可能缺少openai库）")
            results["status"] = "client_not_initialized"
        
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        results["status"] = "error"
        results["error"] = str(e)
    
    return results


def test_local_llm_provider():
    """
    测试本地LLM Provider (Ollama)
    """
    print("\n" + "=" * 60)
    print("测试本地LLM Provider (Ollama)")
    print("=" * 60)
    
    results = {}
    
    try:
        from src.common.llm_service import LocalLLMProvider
        
        # 默认Ollama地址
        base_url = "http://localhost:11434"
        
        # 检查Ollama服务是否运行
        import requests
        try:
            response = requests.get(f"{base_url}/api/tags", timeout=5)
            if response.status_code == 200:
                models = response.json().get("models", [])
                print(f"✅ Ollama服务: 运行中")
                print(f"可用模型: {[m['name'] for m in models]}")
                results["ollama_status"] = "running"
                results["available_models"] = [m['name'] for m in models]
                
                # 测试对话
                if models:
                    provider = LocalLLMProvider(base_url=base_url, model=models[0]['name'])
                    print("\n测试对话功能...")
                    try:
                        response = provider.chat(prompt="你好")
                        print(f"响应: {response[:100]}...")
                        results["chat_test"] = {"status": "成功", "response": response[:100]}
                        print("✅ 对话功能: 正常")
                    except Exception as e:
                        results["chat_test"] = {"status": "失败", "error": str(e)}
                        print(f"❌ 对话功能: 失败 - {e}")
            else:
                print(f"⚠️ Ollama服务: 响应异常 ({response.status_code})")
                results["ollama_status"] = "error"
        except requests.exceptions.ConnectionError:
            print("❌ Ollama服务: 未运行")
            results["ollama_status"] = "not_running"
        except Exception as e:
            print(f"❌ 检查Ollama失败: {e}")
            results["ollama_status"] = "error"
            results["error"] = str(e)
            
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        results["status"] = "error"
        results["error"] = str(e)
    
    return results


def test_qwen_provider():
    """
    测试阿里云千问Provider
    """
    print("\n" + "=" * 60)
    print("测试阿里云千问Provider")
    print("=" * 60)
    
    results = {}
    
    api_key = os.getenv("DASHSCOPE_API_KEY")
    
    if not api_key:
        print("⚠️ DASHSCOPE_API_KEY未设置，跳过测试")
        results["status"] = "skipped"
        results["reason"] = "API密钥未设置"
        return results
    
    try:
        import requests
        
        print(f"API密钥: {api_key[:8]}****{api_key[-4:]}")
        
        # 测试API连接
        url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": "qwen-plus",
            "input": {
                "messages": [
                    {"role": "user", "content": "你好"}
                ]
            }
        }
        
        print("\n测试API连接...")
        response = requests.post(url, headers=headers, json=data, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            print("✅ 千问API: 连接正常")
            results["status"] = "success"
            results["response"] = result
        else:
            print(f"❌ 千问API: 响应异常 ({response.status_code})")
            print(f"错误信息: {response.text}")
            results["status"] = "error"
            results["error"] = response.text
            
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        results["status"] = "error"
        results["error"] = str(e)
    
    return results


def check_embedding_service():
    """
    检查Embedding服务
    """
    print("\n" + "=" * 60)
    print("检查Embedding服务")
    print("=" * 60)
    
    results = {}
    
    try:
        from src.rag.embedding_service import EmbeddingService, EmbeddingConfig
        
        # 获取配置
        config = EmbeddingConfig()
        print(f"模型名称: {config.modelName}")
        print(f"向量维度: {config.dimension}")
        
        # 创建服务
        service = EmbeddingService(config)
        
        # 测试嵌入
        print("\n测试嵌入功能...")
        test_texts = ["这是一个测试句子", "测试嵌入功能"]
        
        try:
            embeddings = service.embed_texts(test_texts)
            print(f"嵌入向量数量: {len(embeddings)}")
            print(f"向量维度: {len(embeddings[0]) if embeddings else 0}")
            
            if embeddings and len(embeddings[0]) > 0:
                print("✅ Embedding服务: 正常")
                results["status"] = "success"
                results["embedding_dim"] = len(embeddings[0])
            else:
                print("❌ Embedding服务: 嵌入结果为空")
                results["status"] = "empty_result"
                
        except Exception as e:
            print(f"❌ 嵌入测试失败: {e}")
            results["status"] = "error"
            results["error"] = str(e)
            
    except Exception as e:
        print(f"❌ 检查失败: {e}")
        results["status"] = "error"
        results["error"] = str(e)
    
    return results


def print_summary(results):
    """
    打印汇总报告
    """
    print("\n" + "=" * 60)
    print("LLM配置检查汇总报告")
    print("=" * 60)
    
    # 环境变量
    env_results = results.get("environment", {})
    env_set = sum(1 for v in env_results.values() if v.get("status") == "已设置")
    print(f"\n环境变量: {env_set}/{len(env_results)} 已设置")
    
    # 配置文件
    config_results = results.get("config_files", {})
    config_ok = sum(1 for v in config_results.values() if v.get("status") == "存在")
    print(f"配置文件: {config_ok}/{len(config_results)} 存在")
    
    # LLM服务
    llm_results = results.get("llm_service", {})
    lib_ok = sum(1 for v in llm_results.values() if v.get("status") == "已安装")
    print(f"依赖库: {lib_ok}/{len(llm_results)} 已安装")
    
    # Provider测试
    print("\nProvider测试结果:")
    
    openai_result = results.get("openai", {})
    if openai_result.get("chat_test", {}).get("status") == "成功":
        print("  ✅ OpenAI Provider: 正常")
    elif openai_result.get("status") == "skipped":
        print("  ⏭️ OpenAI Provider: 跳过（未配置API密钥）")
    else:
        print("  ❌ OpenAI Provider: 异常")
    
    ollama_result = results.get("ollama", {})
    if ollama_result.get("ollama_status") == "running":
        print("  ✅ Ollama (本地LLM): 运行中")
    else:
        print("  ⏭️ Ollama (本地LLM): 未运行")
    
    qwen_result = results.get("qwen", {})
    if qwen_result.get("status") == "success":
        print("  ✅ 千问 Provider: 正常")
    elif qwen_result.get("status") == "skipped":
        print("  ⏭️ 千问 Provider: 跳过（未配置API密钥）")
    else:
        print("  ❌ 千问 Provider: 异常")
    
    # Embedding服务
    embedding_result = results.get("embedding", {})
    if embedding_result.get("status") == "success":
        print(f"  ✅ Embedding服务: 正常 (维度: {embedding_result.get('embedding_dim', 'N/A')})")
    else:
        print("  ❌ Embedding服务: 异常")
    
    # 建议
    print("\n建议:")
    if env_set == 0:
        print("  1. 请设置环境变量（OPENAI_API_KEY 或 DASHSCOPE_API_KEY）")
    if not openai_result.get("chat_test", {}).get("status") == "成功":
        print("  2. 如需使用OpenAI，请确保API密钥正确且有余额")
    if ollama_result.get("ollama_status") != "running":
        print("  3. 如需使用本地LLM，请启动Ollama服务: ollama serve")


def main():
    """
    主函数
    """
    print("=" * 60)
    print("LLM模型配置检查和测试")
    print("=" * 60)
    
    results = {}
    
    # 1. 检查环境变量
    results["environment"] = check_environment_variables()
    
    # 2. 检查配置文件
    results["config_files"] = check_config_files()
    
    # 3. 检查LLM服务依赖
    results["llm_service"] = check_llm_service()
    
    # 4. 测试OpenAI Provider
    results["openai"] = test_openai_provider()
    
    # 5. 测试本地LLM Provider
    results["ollama"] = test_local_llm_provider()
    
    # 6. 测试千问Provider
    results["qwen"] = test_qwen_provider()
    
    # 7. 检查Embedding服务
    results["embedding"] = check_embedding_service()
    
    # 打印汇总
    print_summary(results)
    
    return results


if __name__ == "__main__":
    results = main()
