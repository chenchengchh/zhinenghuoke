"""
使用中文嵌入模型重建向量数据库
"""
import sys
sys.path.insert(0, '.')

import json
import shutil
from pathlib import Path
from datetime import datetime
from src.rag.vector_store import ChromaVectorStore, ChromaConfig
from src.rag.embedding_service import EmbeddingService, EmbeddingConfig

def clean_text(text):
    """清理文本中的元数据标记"""
    if not text:
        return ""
    
    import re
    cleaned = text
    
    # 移除所有元数据标记
    patterns_to_remove = [
        r'【标签】[^【]*',
        r'【关键词】[^【]*',
        r'【别名问题】[^【]*',
        r'【优先级】\d+\s*',
        r'【状态】\S+\s*',
        r'【使用次数】\d+\s*',
        r'【答案】\s*',
        r'【回答】\s*',
        r'─+\s*',
    ]
    
    for pattern in patterns_to_remove:
        cleaned = re.sub(pattern, '', cleaned)
    
    # 去除开头和结尾的特殊字符
    cleaned = re.sub(r'^[\|\s\-─]+', '', cleaned)
    cleaned = re.sub(r'[\|\s\-─]+$', '', cleaned)
    
    # 清理多余空格和换行
    cleaned = ' '.join(cleaned.split())
    cleaned = cleaned.strip()
    
    # 移除结尾的竖线
    cleaned = re.sub(r'\|$', '', cleaned).strip()
    
    return cleaned

def main():
    print("=" * 70)
    print("使用中文嵌入模型重建向量数据库")
    print("=" * 70)
    
    # 加载知识库数据
    kb_path = Path('data/knowledge_unified.json')
    print(f"\n加载知识库数据: {kb_path}")
    with open(kb_path, 'r', encoding='utf-8') as f:
        kb_data = json.load(f)
    
    print(f"知识库条目数: {len(kb_data)}")
    
    # 备份旧的向量数据库
    old_db_path = Path('data/chroma_db')
    backup_path = Path('data/chroma_db_old')
    
    if old_db_path.exists():
        print(f"\n备份旧的向量数据库到: {backup_path}")
        if backup_path.exists():
            shutil.rmtree(backup_path)
        shutil.move(str(old_db_path), str(backup_path))
    
    # 初始化新的向量存储（使用中文嵌入模型）
    print("\n初始化新的向量存储...")
    print("使用模型: BAAI/bge-small-zh-v1.5")
    
    config = ChromaConfig(
        persistDirectory='data/chroma_db',
        collectionPrefix='enterprise_'
    )
    embedding_config = EmbeddingConfig(
        modelName='BAAI/bge-small-zh-v1.5',
        cacheDir='models/embedding'
    )
    embedding = EmbeddingService(embedding_config)
    vector_store = ChromaVectorStore(config, embedding)
    
    # 准备数据
    print("\n准备向量数据...")
    documents = []
    metadatas = []
    ids = []
    
    for item in kb_data:
        item_id = item.get('id')
        question = clean_text(item.get('question', ''))
        answer = clean_text(item.get('answer', ''))
        
        # 跳过无效条目
        if not question or len(question) < 3:
            continue
        if not answer or len(answer) < 10:
            continue
        
        # 创建文档内容
        doc_content = f"问题: {question}\n答案: {answer}"
        
        # 创建元数据
        metadata = {
            'question': question,
            'category': item.get('category', 'general'),
            'source': item.get('source', 'knowledge_base'),
            'keywords': ','.join(item.get('keywords', [])) if isinstance(item.get('keywords'), list) else str(item.get('keywords', ''))
        }
        
        documents.append(doc_content)
        metadatas.append(metadata)
        ids.append(item_id)
    
    print(f"有效条目数: {len(documents)}")
    
    # 批量添加到向量数据库
    print("\n添加向量到数据库...")
    batch_size = 50  # 减小批次大小，中文模型可能需要更多内存
    total_added = 0
    
    for i in range(0, len(documents), batch_size):
        batch_docs = documents[i:i+batch_size]
        batch_metas = metadatas[i:i+batch_size]
        batch_ids = ids[i:i+batch_size]
        
        try:
            # 生成嵌入向量
            embeddings = embedding.embed(batch_docs)
            
            # 添加到集合
            collection = vector_store.getCollection('main', createIfNotExists=True)
            collection.add(
                documents=batch_docs,
                embeddings=embeddings,
                metadatas=batch_metas,
                ids=batch_ids
            )
            
            total_added += len(batch_docs)
            print(f"  已添加 {total_added}/{len(documents)} 条...")
        except Exception as e:
            print(f"  批次 {i//batch_size + 1} 添加失败: {e}")
    
    print(f"\n向量数据库重建完成!")
    print(f"总共添加: {total_added} 条向量")
    
    # 验证
    print("\n验证向量数据库...")
    collection = vector_store.getCollection('main', createIfNotExists=False)
    final_count = collection.count()
    print(f"向量数据库最终文档数: {final_count}")
    
    # 测试搜索
    print("\n测试搜索功能...")
    test_query = "怎么策划一场成功的营销活动"
    results = vector_store.search(
        enterpriseId='main',
        query=test_query,
        topK=3
    )
    
    print(f"\n测试查询: {test_query}")
    print(f"找到 {len(results)} 个结果:")
    for i, (doc, score) in enumerate(results):
        print(f"\n--- 结果 {i+1} (分数: {score:.4f}) ---")
        print(f"内容: {doc['content'][:150]}...")
        print(f"元数据: {doc.get('metadata', {})}")
    
    # 保存重建日志
    log = {
        'timestamp': datetime.now().isoformat(),
        'model': 'BAAI/bge-small-zh-v1.5',
        'knowledge_base_count': len(kb_data),
        'valid_items': len(documents),
        'vectors_added': total_added,
        'final_count': final_count
    }
    
    with open('data/vector_db_rebuild_log_zh.json', 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    
    print(f"\n重建日志已保存: data/vector_db_rebuild_log_zh.json")

if __name__ == '__main__':
    main()
