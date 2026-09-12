import json
import os
import re

def sanitize_kb():
    kb_path = 'data/knowledge_base.json'
    if not os.path.exists(kb_path):
        print(f"Knowledge base not found at {kb_path}")
        return

    with open(kb_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 替换规则
    replacements = {
        r"微信": "联系方式",
        r"加微": "联系",
        r"微信号": "联系账号",
        r"电话": "联系方式",
        r"手机号": "联系号码",
        r"手机": "联系方式",
        r"QQ": "联系账号",
    }

    modified_count = 0
    for item in data:
        answer = item.get('answer', '')
        original_answer = answer
        for pattern, replacement in replacements.items():
            answer = re.sub(pattern, replacement, answer)
        
        if answer != original_answer:
            item['answer'] = answer
            modified_count += 1
            
        # 同时清理关键词和别名
        if 'keywords' in item:
            item['keywords'] = [re.sub(r"微信|电话|手机|QQ", "联系方式", k) for k in item['keywords']]
        if 'aliases' in item:
            item['aliases'] = [re.sub(r"微信|电话|手机|QQ", "联系方式", a) for a in item['aliases']]

    if modified_count > 0:
        backup_path = f"{kb_path}.backup_sensitive_cleanup"
        os.rename(kb_path, backup_path)
        with open(kb_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Successfully sanitized {modified_count} items in knowledge base. Backup created at {backup_path}")
    else:
        print("No sensitive words found in knowledge base.")

if __name__ == "__main__":
    sanitize_kb()
