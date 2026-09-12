# -*- coding: utf-8 -*-
"""
安全模块
提供企业级应用的安全功能
"""
import os
import hashlib
import secrets
import base64
import hmac
import re
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from src.infrastructure.config import get_config
from src.infrastructure.logger import get_logger

logger = get_logger("security")


class EncryptionService:
    """
    加密服务
    
    提供数据加密和解密功能
    """
    
    def __init__(self, secret_key: Optional[str] = None):
        config = get_config()
        self.secret_key = secret_key or config.security.secret_key
        if not self.secret_key:
            import secrets
            self.secret_key = secrets.token_hex(32)
            logger.warning("SECRET_KEY未设置，已生成临时密钥。重启后令牌将失效，请设置环境变量SECRET_KEY")
        self._fernet = self._create_fernet()
    
    def _create_fernet(self) -> Fernet:
        """创建Fernet加密器"""
        key = self.secret_key.encode()
        salt_env = os.getenv("ENCRYPTION_SALT")
        if salt_env:
            salt = salt_env.encode()
        else:
            import hashlib
            salt = hashlib.sha256(b"huoketest_default_salt").digest()
            logger.warning("ENCRYPTION_SALT未设置，使用默认派生盐值。建议设置环境变量ENCRYPTION_SALT")
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(key))
        return Fernet(key)
    
    def encrypt(self, data: str) -> str:
        """
        加密数据
        
        Args:
            data: 待加密的字符串
            
        Returns:
            加密后的字符串
        """
        if not data:
            return ""
        encrypted = self._fernet.encrypt(data.encode())
        return base64.urlsafe_b64encode(encrypted).decode()
    
    def decrypt(self, encrypted_data: str) -> str:
        """
        解密数据
        
        Args:
            encrypted_data: 加密的字符串
            
        Returns:
            解密后的原始字符串
        """
        if not encrypted_data:
            return ""
        try:
            decoded = base64.urlsafe_b64decode(encrypted_data.encode())
            decrypted = self._fernet.decrypt(decoded)
            return decrypted.decode()
        except Exception as e:
            logger.error(f"解密失败: {e}")
            raise ValueError("解密失败，数据可能已损坏")
    
    def encrypt_dict(self, data: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
        """
        加密字典中的指定字段
        
        Args:
            data: 原始字典
            fields: 需要加密的字段列表
            
        Returns:
            加密后的字典
        """
        result = data.copy()
        for field in fields:
            if field in result and result[field]:
                result[field] = self.encrypt(str(result[field]))
        return result
    
    def decrypt_dict(self, data: Dict[str, Any], fields: List[str]) -> Dict[str, Any]:
        """
        解密字典中的指定字段
        
        Args:
            data: 加密的字典
            fields: 需要解密的字段列表
            
        Returns:
            解密后的字典
        """
        result = data.copy()
        for field in fields:
            if field in result and result[field]:
                try:
                    result[field] = self.decrypt(result[field])
                except (ValueError, Exception) as e:
                    logger.warning(f"解密字段 {field} 失败: {e}")
        return result


class DataMasker:
    """
    数据脱敏服务
    
    提供各种敏感数据的脱敏处理
    """
    
    @staticmethod
    def mask_phone(phone: str) -> str:
        """
        手机号脱敏
        
        示例: 13812345678 -> 138****5678
        """
        if not phone or len(phone) < 7:
            return phone
        return phone[:3] + "****" + phone[-4:]
    
    @staticmethod
    def mask_email(email: str) -> str:
        """
        邮箱脱敏
        
        示例: test@example.com -> t***@example.com
        """
        if not email or "@" not in email:
            return email
        parts = email.split("@")
        if len(parts[0]) <= 1:
            return email
        return parts[0][0] + "***@" + parts[1]
    
    @staticmethod
    def mask_id_card(id_card: str) -> str:
        """
        身份证号脱敏
        
        示例: 110101199001011234 -> 110101********1234
        """
        if not id_card or len(id_card) < 8:
            return id_card
        return id_card[:6] + "********" + id_card[-4:]
    
    @staticmethod
    def mask_bank_card(card: str) -> str:
        """
        银行卡号脱敏
        
        示例: 6222021234567890123 -> 6222 **** **** 0123
        """
        if not card or len(card) < 8:
            return card
        return card[:4] + " **** **** " + card[-4:]
    
    @staticmethod
    def mask_name(name: str) -> str:
        """
        姓名脱敏
        
        示例: 张三 -> 张*，李四四 -> 李**
        """
        if not name:
            return name
        if len(name) == 1:
            return name
        return name[0] + "*" * (len(name) - 1)
    
    @staticmethod
    def mask_password(password: str) -> str:
        """
        密码脱敏
        
        返回固定长度的星号
        """
        return "******"
    
    @staticmethod
    def mask_token(token: str) -> str:
        """
        Token脱敏
        
        示例: abcdefghijklmnop -> abcd...mnop
        """
        if not token or len(token) < 8:
            return "***"
        return token[:4] + "..." + token[-4:]
    
    @classmethod
    def auto_mask(cls, data: Dict[str, Any], sensitive_fields: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """
        自动脱敏字典中的敏感字段
        
        Args:
            data: 原始数据
            sensitive_fields: 敏感字段映射 {字段名: 类型}
                类型: phone, email, id_card, bank_card, name, password, token
            
        Returns:
            脱敏后的数据
        """
        if not sensitive_fields:
            sensitive_fields = {
                "phone": "phone",
                "mobile": "phone",
                "email": "email",
                "id_card": "id_card",
                "idCard": "id_card",
                "bank_card": "bank_card",
                "bankCard": "bank_card",
                "name": "name",
                "real_name": "name",
                "realName": "name",
                "password": "password",
                "pwd": "password",
                "token": "token",
                "access_token": "token",
                "refresh_token": "token"
            }
        
        result = data.copy()
        mask_methods = {
            "phone": cls.mask_phone,
            "email": cls.mask_email,
            "id_card": cls.mask_id_card,
            "bank_card": cls.mask_bank_card,
            "name": cls.mask_name,
            "password": cls.mask_password,
            "token": cls.mask_token
        }
        
        for field, field_type in sensitive_fields.items():
            if field in result and result[field]:
                mask_method = mask_methods.get(field_type)
                if mask_method:
                    result[field] = mask_method(str(result[field]))
        
        return result


class PasswordHasher:
    """
    密码哈希服务
    
    提供安全的密码哈希和验证功能
    """
    
    ITERATIONS = 100000
    ALGORITHM = "sha256"
    
    @classmethod
    def hash_password(cls, password: str, salt: Optional[str] = None) -> Tuple[str, str]:
        """
        哈希密码
        
        Args:
            password: 原始密码
            salt: 盐值（可选，不提供则自动生成）
            
        Returns:
            (哈希后的密码, 盐值)
        """
        if not salt:
            salt = secrets.token_hex(16)
        
        hash_value = hashlib.pbkdf2_hmac(
            cls.ALGORITHM,
            password.encode(),
            salt.encode(),
            cls.ITERATIONS
        )
        hashed = base64.urlsafe_b64encode(hash_value).decode()
        return hashed, salt
    
    @classmethod
    def verify_password(cls, password: str, hashed: str, salt: str) -> bool:
        """
        验证密码
        
        Args:
            password: 待验证的密码
            hashed: 哈希后的密码
            salt: 盐值
            
        Returns:
            是否匹配
        """
        new_hash, _ = cls.hash_password(password, salt)
        return hmac.compare_digest(new_hash, hashed)
    
    @classmethod
    def validate_password_strength(cls, password: str) -> Tuple[bool, List[str]]:
        """
        验证密码强度
        
        Args:
            password: 待验证的密码
            
        Returns:
            (是否通过, 错误信息列表)
        """
        config = get_config()
        errors = []
        
        if len(password) < config.security.password_min_length:
            errors.append(f"密码长度至少{config.security.password_min_length}位")
        
        if config.security.password_require_uppercase and not re.search(r"[A-Z]", password):
            errors.append("密码需要包含大写字母")
        
        if config.security.password_require_lowercase and not re.search(r"[a-z]", password):
            errors.append("密码需要包含小写字母")
        
        if config.security.password_require_digit and not re.search(r"\d", password):
            errors.append("密码需要包含数字")
        
        if config.security.password_require_special and not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
            errors.append("密码需要包含特殊字符")
        
        return len(errors) == 0, errors


class TokenService:
    """
    Token服务
    
    提供JWT令牌的生成和验证功能
    """
    
    def __init__(self, secret_key: Optional[str] = None):
        config = get_config()
        self.secret_key = secret_key or config.security.secret_key
        self.algorithm = config.security.algorithm
        self.access_token_expire = config.security.access_token_expire_minutes
        self.refresh_token_expire = config.security.refresh_token_expire_days
    
    def generate_access_token(
        self,
        user_id: str,
        enterprise_id: Optional[str] = None,
        roles: Optional[List[str]] = None,
        extra_data: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        生成访问令牌
        
        Args:
            user_id: 用户ID
            enterprise_id: 企业ID
            roles: 角色列表
            extra_data: 额外数据
            
        Returns:
            JWT令牌
        """
        import jwt
        
        now = datetime.now(timezone.utc)
        payload = {
            "sub": user_id,
            "type": "access",
            "iat": now,
            "exp": now + timedelta(minutes=self.access_token_expire),
            "jti": secrets.token_hex(16)
        }
        
        if enterprise_id:
            payload["enterprise_id"] = enterprise_id
        if roles:
            payload["roles"] = roles
        if extra_data:
            reserved_keys = {"sub", "type", "iat", "exp", "jti"}
            safe_extra = {k: v for k, v in extra_data.items() if k not in reserved_keys}
            payload.update(safe_extra)
        
        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)
    
    def generate_refresh_token(self, user_id: str) -> str:
        """
        生成刷新令牌
        
        Args:
            user_id: 用户ID
            
        Returns:
            刷新令牌
        """
        import jwt
        
        now = datetime.now(timezone.utc)
        payload = {
            "sub": user_id,
            "type": "refresh",
            "iat": now,
            "exp": now + timedelta(days=self.refresh_token_expire),
            "jti": secrets.token_hex(16)
        }
        
        return jwt.encode(payload, self.secret_key, algorithm=self.algorithm)
    
    def verify_token(self, token: str, token_type: str = "access") -> Optional[Dict[str, Any]]:
        """
        验证令牌
        
        Args:
            token: JWT令牌
            token_type: 令牌类型 (access/refresh)
            
        Returns:
            解码后的载荷，验证失败返回None
        """
        import jwt
        from jwt import ExpiredSignatureError, InvalidTokenError
        
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
            
            if payload.get("type") != token_type:
                logger.warning(f"令牌类型不匹配: 期望{token_type}, 实际{payload.get('type')}")
                return None
            
            return payload
        except ExpiredSignatureError:
            logger.warning("令牌已过期")
            return None
        except InvalidTokenError as e:
            logger.warning(f"无效令牌: {e}")
            return None
    
    def refresh_access_token(self, refresh_token: str) -> Optional[str]:
        """
        使用刷新令牌获取新的访问令牌
        
        Args:
            refresh_token: 刷新令牌
            
        Returns:
            新的访问令牌，失败返回None
        """
        payload = self.verify_token(refresh_token, "refresh")
        if not payload:
            return None
        
        return self.generate_access_token(
            user_id=payload["sub"],
            enterprise_id=payload.get("enterprise_id"),
            roles=payload.get("roles")
        )


@lru_cache(maxsize=1)
def get_encryption_service() -> EncryptionService:
    """获取加密服务实例"""
    return EncryptionService()


@lru_cache(maxsize=1)
def get_token_service() -> TokenService:
    """获取Token服务实例"""
    return TokenService()
