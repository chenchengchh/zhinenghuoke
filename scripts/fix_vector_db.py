"""
修复向量数据库 - 删除旧集合并同步知识库
"""
import sys
sys.path.insert(0, '.')

import json
import shutil
from pathlib import Path
from datetime import datetime
from src.rag.vector_store import ChromaVectorStore, ChromaConfig, get_chroma_client
from src.rag.embedding_service import EmbeddingService, EmbeddingConfig

def main():
    print("=" * 70)
    print("修复向量数据库")
    print("=" * 70)
    
    # 获取ChromaDB客户端
    client = get_chroma_client('data/chroma_db')
    
    # 1. 删除旧的default集合
    print("\n1. 删除旧的enterprise_default集合...")
    try:
        client.delete_collection('enterprise_default')
        print("  ✓ 已删除enterprise_default集合")
    except Exception as e:
        print(f"  删除失败: {e}")
    
    # 2. 检查main集合
    print("\n2. 检查enterprise_main集合...")
    main_coll = client.get_collection('enterprise_main')
    print(f"  文档数: {main_coll.count()}")
    
    sample = main_coll.get(limit=1, include=['embeddings'])
    if sample.get('embeddings') is not None and len(sample['embeddings']) > 0:
        emb_dim = len(sample['embeddings'][0])
        print(f"  嵌入维度: {emb_dim}")
    
    # 3. 加载知识库并同步缺失的条目
    print("\n3. 同步知识库中缺失的条目...")
    kb_path = Path('data/knowledge_unified.json')
    with open(kb_path, 'r', encoding='utf-8') as f:
        kb_data = json.load(f)
    
    # 获取main集合中已有的ID
    all_data = main_coll.get(include=['documents'])
    existing_ids = set(all_data['ids'])
    
    print(f"  知识库条目数: {len(kb_data)}")
    print(f"  向量库条目数: {len(existing_ids)}")
    
    # 找出缺失的条目
    missing_items = []
    for item in kb_data:
        item_id = item.get('id')
        if item_id not in existing_ids:
            missing_items.append(item)
    
    print(f"  缺失条目数: {len(missing_items)}")
    
    if missing_items:
        # 初始化嵌入服务
        embedding_config = EmbeddingConfig(
            modelName='BAAI/bge-small-zh-v1.5',
            cacheDir='models/embedding'
        )
        embedding = EmbeddingService(embedding_config)
        
        config = ChromaConfig(
            persistDirectory='data/chroma_db',
            collectionPrefix='enterprise_'
        )
        vector_store = ChromaVectorStore(config, embedding)
        
        # 添加缺失的条目
        documents = []
        metadatas = []
        ids = []
        
        for item in missing_items:
            question = item.get('question', '').strip()
            answer = item.get('answer', '').strip()
            
            if not question or len(question) < 3:
                continue
            if not answer or len(answer) < 10:
                continue
            
            # 清理元数据标记
            import re
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
                question = re.sub(pattern, '', question)
                answer = re.sub(pattern, '', answer)
            
            question = ' '.join(question.split()).strip()
            answer = ' '.join(answer.split()).strip()
            
            if len(question) < 3 or len(answer) < 10:
                continue
            
            doc_content = f"问题: {question}\n答案: {answer}"
            
            metadata = {
                'question': question,
                'category': item.get('category', 'general'),
                'source': item.get('source', 'knowledge_base'),
                'keywords': ','.join(item.get('keywords', [])) if isinstance(item.get('keywords'), list) else str(item.get('keywords', ''))
            }
            
            documents.append(doc_content)
            metadatas.append(metadata)
            ids.append(item.get('id'))
        
        print(f"  准备添加 {len(documents)} 条新条目...")
        
        if documents:
            # 批量添加
            batch_size = 50
            total_added = 0
            
            for i in range(0, len(documents), batch_size):
                batch_docs = documents[i:i+batch_size]
                batch_metas = metadatas[i:i+batch_size]
                batch_ids = ids[i:i+batch_size]
                
                try:
                    embeddings = embedding.embed(batch_docs)
                    
                    main_coll.add(
                        documents=batch_docs,
                        embeddings=embeddings,
                        metadatas=batch_metas,
                        ids=batch_ids
                    )
                    
                    total_added += len(batch_docs)
                    print(f"    已添加 {total_added}/{len(documents)} 条...")
                except Exception as e:
                    print(f"    批次添加失败: {e}")
            
            print(f"  ✓ 共添加 {total_added} 条新条目")
    
    # 4. 验证最终结果
    print("\n4. 验证最终结果...")
    final_count = main_coll.count()
    print(f"  最终文档数: {final_count}")
    
    # 保存修复日志
    log = {
        'timestamp': datetime.now().isoformat(),
        'final_count': final_count,
        'missing_items_added': len(missing_items) if missing_items else 0
    }
    
    with open('data/vector_db_fix_log.json', 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    
    print("\n修复完成!")

if __name__ == '__main__':
    main()
