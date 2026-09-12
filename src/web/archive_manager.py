"""
系统状态持久化管理器

提供可靠的应用状态存档和恢复功能，支持：
- 手动/自动定期存档
- 状态快照与恢复
- 存档完整性校验（SHA256 + CRC32双校验）
- 多版本存档管理（FIFO策略）
- 数据压缩存储（gzip）
- 存档空间管理
- 并发安全保护

使用场景：
1. 系统升级前的状态备份
2. 异常恢复时的数据回滚
3. 定期数据快照
4. 调试问题复现
5. 数据迁移验证
"""

import json
import os
import gzip
import zlib
import shutil
import hashlib
import threading
from datetime import datetime, timedelta
from typing import Any, Optional, Dict, List, Callable
from pathlib import Path
from contextlib import contextmanager
from loguru import logger

from src.common.chat_store import ChatStoreFacade

# 存档目录配置
ARCHIVE_DIR = Path("data/archives")
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

# 存档限制配置
MAX_ARCHIVES: int = 10              # 最大保留存档数量
MAX_ARCHIVE_SIZE_MB: int = 50       # 单个存档最大大小（MB）
COMPRESS_THRESHOLD_BYTES: int = 1024  # 超过此大小的存档自动压缩


class ArchiveError(Exception):
    """存档操作异常基类"""
    pass


class ArchiveValidationError(ArchiveError):
    """存档校验失败异常"""
    pass


class ArchiveNotFoundError(ArchiveError):
    """存档不存在异常"""
    pass


class SystemArchiveManager:
    """
    系统状态存档管理器（线程安全单例）
    
    功能特性：
    - 原子写入：先写临时文件再重命名，防止数据损坏
    - 双重校验：SHA256 + CRC32，确保数据完整性
    - 自动压缩：大文件自动使用gzip压缩存储
    - 空间管理：自动清理旧存档，控制总空间占用
    - 并发安全：线程锁保护关键操作
    
    Example:
        >>> manager = get_archive_manager()
        >>> result = manager.create_archive({"key": "value"}, description="测试存档")
        >>> print(result["archive_id"])
        "20240115_103000"
        
        >>> data = manager.load_archive("20240115_103000")
        >>> print(data)
        {"key": "value", ...}
    """
    
    def __init__(self, archive_dir: Optional[Path] = None):
        """
        初始化存档管理器
        
        Args:
            archive_dir: 存档根目录，默认为 data/archives
        """
        self.archive_dir: Path = archive_dir or ARCHIVE_DIR
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        
        # 内部状态
        self._current_archive_id: Optional[str] = None
        self._last_archive_time: Optional[datetime] = None
        
        # 线程安全锁
        self._lock: threading.RLock = threading.RLock()
        
        # 统计信息
        self._stats: Dict[str, int] = {
            "total_created": 0,
            "total_loaded": 0,
            "total_deleted": 0,
            "total_errors": 0,
            "bytes_written": 0,
            "bytes_read": 0
        }
        
        logger.info(f"存档管理器初始化完成: 目录={self.archive_dir}")
    
    def create_archive(
        self,
        state_data: Dict[str, Any],
        archive_type: str = "manual",
        description: str = "",
        compress: bool | None = None,
        metadata_extra: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """
        创建系统状态存档
        
        Args:
            state_data: 需要存档的状态数据字典
            archive_type: 存档类型 (manual/auto/critical/scheduled)
            description: 存档描述信息
            compress: 是否压缩存储（None=根据大小自动判断）
            metadata_extra: 额外的元数据字段
            
        Returns:
            dict: 存档结果 {
                "success": bool,
                "archive_id": str,
                "path": str,
                "size_bytes": int,
                "compressed": bool,
                "timestamp": str,
                ...
            }
            
        Raises:
            ArchiveError: 创建过程中发生严重错误时抛出
        """
        with self._lock:
            try:
                timestamp = datetime.now()
                archive_id = self._generate_archive_id(timestamp, archive_type)
                
                # 创建存档目录
                archive_path = self.archive_dir / archive_id
                archive_path.mkdir(exist_ok=True)
                
                # 准备元数据
                json_bytes = json.dumps(
                    state_data, 
                    ensure_ascii=False, 
                    default=str,
                    separators=(',', ':')  # 紧凑格式减少体积
                ).encode('utf-8')
                
                # 决定是否压缩
                raw_size = len(json_bytes)
                should_compress = (
                    compress if compress is not None 
                    else raw_size >= COMPRESS_THRESHOLD_BYTES
                )
                
                # 构建基础元数据（不含校验值）
                archive_metadata = {
                    "archive_id": archive_id,
                    "timestamp": timestamp.isoformat(),
                    "type": archive_type,
                    "description": description or f"{archive_type.capitalize()} - {timestamp.strftime('%Y-%m-%d %H:%M')}",
                    "version": "2.0",
                    "data_keys": list(state_data.keys()),
                    "raw_size_bytes": raw_size,
                    "compressed": should_compress,
                    "created_by": "system",
                    **(metadata_extra or {})
                }
                
                # 构建完整存档内容
                full_archive = {
                    "_metadata": archive_metadata,
                    "_schema_version": "2.0",
                    "state": state_data
                }
                
                # 计算双重校验值（基于最终存储的state数据，使用sort_keys确保一致性）
                state_json_for_hash = json.dumps(
                    state_data, 
                    ensure_ascii=False, 
                    sort_keys=True, 
                    default=str
                ).encode('utf-8')
                
                sha256_hash = hashlib.sha256(state_json_for_hash).hexdigest()[:16]
                crc32_value = f"{zlib.crc32(state_json_for_hash) & 0xFFFFFFFF:08x}"
                
                # 更新元数据中的校验值
                archive_metadata["sha256_checksum"] = sha256_hash
                archive_metadata["crc32"] = crc32_value
                full_archive["_metadata"] = archive_metadata
                
                # 写入文件（原子操作）
                if should_compress:
                    state_file = archive_path / "state.json.gz"
                    temp_file = archive_path / "state.json.gz.tmp"
                    
                    with open(temp_file, 'wb') as f:
                        f.write(gzip.compress(
                            json.dumps(full_archive, ensure_ascii=False).encode('utf-8'),
                            compresslevel=6  # 平衡压缩率和速度
                        ))
                else:
                    state_file = archive_path / "state.json"
                    temp_file = archive_path / "state.json.tmp"
                    
                    with open(temp_file, 'w', encoding='utf-8') as f:
                        json.dump(full_archive, f, ensure_ascii=False, indent=2)
                
                # 原子重命名
                temp_file.rename(state_file)
                
                final_size = state_file.stat().st_size
                
                # 更新索引和统计
                self._update_latest_archive(archive_id, archive_metadata)
                self._cleanup_old_archives()
                
                # 更新内部状态
                self._current_archive_id = archive_id
                self._last_archive_time = timestamp
                self._stats["total_created"] += 1
                self._stats["bytes_written"] += final_size
                
                result = {
                    "success": True,
                    "archive_id": archive_id,
                    "path": str(archive_path),
                    "size_bytes": final_size,
                    "raw_size_bytes": raw_size,
                    "compression_ratio": round(final_size / raw_size * 100, 1) if raw_size > 0 else 100,
                    "compressed": should_compress,
                    "timestamp": timestamp.isoformat(),
                    "data_keys": list(state_data.keys()),
                    "checksum": sha256_hash
                }
                
                logger.info(
                    f"存档创建成功: {archive_id} "
                    f"(原始={raw_size:,}B, 存储={final_size:,}B"
                    + (f", 压缩率={result['compression_ratio']}%" if should_compress else "") + ")"
                )
                
                return result
                
            except Exception as e:
                self._stats["total_errors"] += 1
                logger.error(f"创建存档失败: {e}", exc_info=True)
                
                return {
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "archive_id": None
                }
    
    def load_archive(self, archive_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        加载指定存档或最新存档
        
        加载流程：
        1. 定位存档文件
        2. 根据扩展名选择解压方式
        3. 解析JSON内容
        4. 双重校验完整性
        5. 返回状态数据
        
        Args:
            archive_id: 存档ID，None表示加载最新存档
            
        Returns:
            dict: 存档的状态数据，失败返回None
            
        Raises:
            ArchiveNotFoundError: 存档不存在
            ArchiveValidationError: 校验失败
        """
        with self._lock:
            try:
                # 确定目标存档ID
                if archive_id is None:
                    archive_id = self.get_latest_archive_id()
                    
                if not archive_id:
                    logger.warning("没有可用的存档")
                    return None
                
                # 定位并读取文件
                archive_path, state_file = self._locate_archive_file(archive_id)
                
                if not state_file.exists():
                    raise ArchiveNotFoundError(f"存档文件不存在: {state_file}")
                
                # 根据格式读取
                is_compressed = state_file.suffix == '.gz'
                
                if is_compressed:
                    with gzip.open(state_file, 'rb') as f:
                        content = f.read().decode('utf-8')
                else:
                    with open(state_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                
                archive_data = json.loads(content)
                
                # 验证完整性
                self._verify_archive_integrity(archive_id, archive_data)
                
                # 提取状态数据
                state_data = archive_data.get("state", {})
                
                self._stats["total_loaded"] += 1
                self._stats["bytes_read"] += state_file.stat().st_size
                
                logger.info(f"存档加载成功: {archive_id} ({len(state_data)} 个键)")
                
                return state_data
                
            except (ArchiveNotFoundError, ArchiveValidationError) as e:
                logger.error(f"加载存档失败: {e}")
                raise
            except Exception as e:
                self._stats["total_errors"] += 1
                logger.error(f"加载存档异常: {e}", exc_info=True)
                return None
    
    def get_latest_archive_id(self) -> Optional[str]:
        """获取最新存档的ID"""
        index_file = self.archive_dir / "latest.json"
        
        # 优先从索引文件获取
        if index_file.exists():
            try:
                with open(index_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return data.get("archive_id")
            except Exception as e:
                logger.debug(f"读取索引文件失败: {e}")
        
        # 回退到扫描目录
        archives = sorted(
            self.archive_dir.glob("*"),
            key=lambda p: p.stat().st_mtime if p.is_dir() else 0,
            reverse=True
        )
        
        for archive in archives:
            if archive.is_dir() and self._has_valid_state_file(archive):
                return archive.name
        
        return None
    
    def list_archives(
        self,
        include_invalid: bool = False,
        sort_by: str = "time"  # time/size/name
    ) -> List[Dict[str, Any]]:
        """
        列出所有可用存档
        
        Args:
            include_invalid: 是否包含损坏的存档
            sort_by: 排序方式 (time/size/name)
            
        Returns:
            list: 存档信息列表（按时间倒序）
        """
        archives = []
        
        for archive_dir in sorted(self.archive_dir.iterdir(), reverse=True):
            if not archive_dir.is_dir():
                continue
            
            info = self._get_archive_info(archive_dir)
            
            if info and (info.get("is_valid") or include_invalid):
                archives.append(info)
        
        # 排序
        sort_key_map = {
            "time": lambda x: x.get("timestamp", ""),
            "size": lambda x: -(x.get("size_bytes", 0)),
            "name": lambda x: x.get("archive_id", "")
        }
        
        if sort_by in sort_key_map:
            archives.sort(key=sort_key_map[sort_by])
        
        return archives
    
    def delete_archive(self, archive_id: str, force: bool = False) -> bool:
        """
        删除指定存档
        
        Args:
            archive_id: 存档ID
            force: 是否强制删除（包括最新存档）
            
        Returns:
            bool: 是否删除成功
            
        Raises:
            ArchiveError: 删除操作被拒绝时
        """
        with self._lock:
            try:
                archive_path = self.archive_dir / archive_id
                
                if not archive_path.exists():
                    raise ArchiveNotFoundError(f"存档不存在: {archive_id}")
                
                # 保护最新存档（除非强制删除）
                if not force:
                    latest_id = self.get_latest_archive_id()
                    if archive_id == latest_id:
                        raise ArchiveError("不允许删除最新存档（如需删除请使用 force=True）")
                
                # 执行删除
                shutil.rmtree(archive_path)
                
                self._stats["total_deleted"] += 1
                logger.info(f"存档已删除: {archive_id}")
                
                return True
                
            except (ArchiveNotFoundError, ArchiveError) as e:
                logger.warning(f"删除存档失败: {e}")
                raise
            except Exception as e:
                self._stats["total_errors"] += 1
                logger.error(f"删除存档异常: {e}", exc_info=True)
                return False
    
    def verify_archive(self, archive_id: str) -> Dict[str, Any]:
        """
        验证存档完整性
        
        验证项目：
        1. 文件存在性检查
        2. JSON格式有效性
        3. SHA256校验和匹配
        4. CRC32校验匹配
        5. 必要字段存在性
        
        Args:
            archive_id: 存档ID
            
        Returns:
            dict: 验证结果 {
                "valid": bool,
                "checks": [...],
                ...
            }
        """
        try:
            _, state_file = self._locate_archive_file(archive_id)
            
            if not state_file.exists():
                return {"valid": False, "error": "存档文件不存在", "checks": []}
            
            # 读取并解析
            is_compressed = state_file.suffix == '.gz'
            
            if is_compressed:
                with gzip.open(state_file, 'rb') as f:
                    content = f.read().decode('utf-8')
            else:
                with open(state_file, 'r', encoding='utf-8') as f:
                    content = f.read()
            
            data = json.loads(content)
            checks = []
            
            # 执行各项检查
            metadata = data.get("_metadata", {})
            state = data.get("state", {})
            
            # 检查1：SHA256
            sha_stored = metadata.get("sha256_checksum")
            if sha_stored:
                sha_current = self._calculate_sha256(state)
                sha_valid = sha_stored == sha_current
                checks.append({
                    "name": "SHA256校验",
                    "passed": sha_valid,
                    "detail": f"stored={sha_stored}, computed={sha_current}"
                })
            else:
                checks.append({"name": "SHA256校验", "passed": None, "detail": "无校验和"})
            
            # 检查2：CRC32
            crc_stored = metadata.get("crc32")
            if crc_stored:
                crc_current = self._calculate_crc32(state)
                crc_valid = crc_stored == crc_current
                checks.append({
                    "name": "CRC32校验",
                    "passed": crc_valid,
                    "detail": f"stored={crc_stored}, computed={crc_current}"
                })
            else:
                checks.append({"name": "CRC32校验", "passed": None, "detail": "无校验码"})
            
            # 检查3：必要字段
            has_data = bool(state)
            checks.append({
                "name": "数据完整性",
                "passed": has_data,
                "detail": f"{len(state)} 个键" if has_data else "空数据"
            })
            
            # 综合判定
            all_passed = [c for c in checks if c["passed"] is not False]
            valid = len(all_passed) > 0 and all(c["passed"] for c in all_passed if c["passed"] is not None)
            
            return {
                "valid": valid,
                "archive_id": archive_id,
                "file_size": state_file.stat().st_size,
                "compressed": is_compressed,
                "data_keys": list(state.keys()) if state else [],
                "checks": checks,
                "check_summary": {
                    "total": len(checks),
                    "passed": sum(1 for c in checks if c["passed"]),
                    "skipped": sum(1 for c in checks if c["passed"] is None),
                    "failed": sum(1 for c in checks if c["passed"] is False)
                }
            }
            
        except Exception as e:
            return {"valid": False, "error": str(e), "checks": []}
    
    def get_storage_stats(self) -> Dict[str, Any]:
        """获取存储空间统计"""
        total_size = 0
        archive_count = 0
        
        for item in self.archive_dir.iterdir():
            if item.is_dir() and self._has_valid_state_file(item):
                archive_count += 1
                total_size += sum(f.stat().st_size for f in item.rglob('*') if f.is_file())
        
        return {
            "archive_count": archive_count,
            "max_archives": MAX_ARCHIVES,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "max_size_mb": MAX_ARCHIVE_SIZE_MB * MAX_ARCHIVES,
            "usage_percent": round(total_size / (MAX_ARCHIVE_SIZE_MB * MAX_ARCHIVES * 1024 * 1024) * 100, 1),
            "operations_stats": self._stats.copy()
        }
    
    def cleanup_all_expired(self, max_age_days: int = 30) -> Dict[str, Any]:
        """
        清理过期存档
        
        Args:
            max_age_days: 最大保留天数
            
        Returns:
            dict: 清理结果
        """
        deleted = []
        errors = []
        cutoff = datetime.now() - timedelta(days=max_age_days)
        
        for archive_info in self.list_archives(include_invalid=True):
            try:
                ts_str = archive_info.get("timestamp")
                if not ts_str:
                    continue
                    
                created_at = datetime.fromisoformat(ts_str)
                
                if created_at < cutoff:
                    aid = archive_info["archive_id"]
                    # 不允许删除最新的
                    if aid != self.get_latest_archive_id():
                        self.delete_archive(aid, force=True)
                        deleted.append(aid)
                        
            except Exception as e:
                errors.append({"id": archive_info.get("archive_id"), "error": str(e)})
        
        return {
            "deleted_count": len(deleted),
            "deleted_ids": deleted,
            "errors": errors,
            "max_age_days": max_age_days
        }
    
    @contextmanager
    def atomic_write(self, file_path: Path, mode: str = 'w'):
        """
        原子写入上下文管理器
        
        Example:
            with manager.atomic_write(path) as f:
                json.dump(data, f)
            # 文件已自动重命名到位
        """
        temp_path = file_path.with_suffix(file_path.suffix + '.tmp')
        
        try:
            f = open(temp_path, mode, encoding='utf-8' if 'b' not in mode else None)
            yield f
            f.close()
            temp_path.rename(file_path)
        except Exception:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            raise
    
    # ==================== 私有方法 ====================
    
    def _generate_archive_id(self, timestamp: datetime, archive_type: str) -> str:
        """生成存档ID"""
        base_id = timestamp.strftime("%Y%m%d_%H%M%S")
        
        if archive_type == "critical":
            return f"CRITICAL_{base_id}"
        elif archive_type == "scheduled":
            return f"AUTO_{base_id}"
        
        return base_id
    
    def _locate_archive_file(self, archive_id: str) -> tuple[Path, Path]:
        """定位存档目录和状态文件"""
        archive_path = self.archive_dir / archive_id
        
        # 尝试两种可能的文件名
        gz_path = archive_path / "state.json.gz"
        normal_path = archive_path / "state.json"
        
        if gz_path.exists():
            return archive_path, gz_path
        elif normal_path.exists():
            return archive_path, normal_path
        else:
            return archive_path, normal_path  # 返回默认路径，调用方处理不存在的情况
    
    def _has_valid_state_file(self, dir_path: Path) -> bool:
        """检查目录是否有有效的状态文件"""
        return (dir_path / "state.json").exists() or (dir_path / "state.json.gz").exists()
    
    def _get_archive_info(self, archive_dir: Path) -> Optional[Dict[str, Any]]:
        """获取单个存档的信息摘要"""
        state_file = None
        
        for name in ["state.json", "state.json.gz"]:
            candidate = archive_dir / name
            if candidate.exists():
                state_file = candidate
                break
        
        if not state_file:
            return None
        
        try:
            with (gzip.open(state_file, 'rb') if state_file.suffix == '.gz' 
                  else open(state_file, 'r', encoding='utf-8')) as f:
                data = json.load(f)
            
            metadata = data.get("_metadata", {})
            
            return {
                "archive_id": archive_dir.name,
                "timestamp": metadata.get("timestamp"),
                "type": metadata.get("type", "unknown"),
                "description": metadata.get("description", ""),
                "size_bytes": state_file.stat().st_size,
                "compressed": state_file.suffix == '.gz',
                "data_keys": metadata.get("data_keys", []),
                "is_valid": True
            }
            
        except Exception as e:
            return {
                "archive_id": archive_dir.name,
                "error": str(e),
                "is_valid": False
            }
    
    def _verify_archive_integrity(self, archive_id: str, data: Dict) -> None:
        """验证存档完整性（内部方法）"""
        metadata = data.get("_metadata", {})
        state = data.get("state", {})
        
        # SHA256校验
        stored_sha = metadata.get("sha256_checksum")
        if stored_sha:
            current_sha = self._calculate_sha256(state)
            if stored_sha != current_sha:
                raise ArchiveValidationError(
                    f"存档校验失败: SHA256不匹配 "
                    f"(expected={stored_sha}, got={current_sha})"
                )
        
        # CRC32校验
        stored_crc = metadata.get("crc32")
        if stored_crc:
            current_crc = self._calculate_crc32(state)
            if stored_crc != current_crc:
                raise ArchiveValidationError(
                    f"存档校验失败: CRC32不匹配 "
                    f"(expected={stored_crc}, got={current_crc})"
                )
    
    def _calculate_sha256(self, data: Any) -> str:
        """计算数据的SHA256哈希（前16位）"""
        json_str = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(json_str.encode('utf-8')).hexdigest()[:16]
    
    def _calculate_crc32(self, data: Any) -> str:
        """计算数据的CRC32校验码"""
        json_str = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
        value = zlib.crc32(json_str.encode('utf-8'))
        return f"{value & 0xFFFFFFFF:08x}"
    
    def _update_latest_archive(self, archive_id: str, metadata: Dict[str, Any]) -> None:
        """更新最新存档索引"""
        index_file = self.archive_dir / "latest.json"
        index_data = {
            "archive_id": archive_id,
            "updated_at": datetime.now().isoformat(),
            "metadata": metadata
        }
        
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)
    
    def _cleanup_old_archives(self) -> None:
        """清理超出数量限制的旧存档"""
        archives = [
            d for d in self.archive_dir.iterdir() 
            if d.is_dir() and self._has_valid_state_file(d)
        ]
        
        # 按修改时间排序（新的在前）
        archives.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        
        # 保留最新的N个
        if len(archives) > MAX_ARCHIVES:
            for old_archive in archives[MAX_ARCHIVES:]:
                try:
                    size_before = sum(f.stat().st_size for f in old_archive.rglob('*'))
                    shutil.rmtree(old_archive)
                    logger.info(
                        f"已清理旧存档: {old_archive.name} "
                        f"(释放 {size_before / 1024:.1f} KB)"
                    )
                except Exception as e:
                    logger.error(f"清理存档失败 {old_archive.name}: {e}")


# 全局单例实例
_archive_manager_instance: Optional[SystemArchiveManager] = None
_manager_lock: threading.Lock = threading.Lock()


def get_archive_manager() -> SystemArchiveManager:
    """
    获取全局存档管理器实例（线程安全单例模式）
    
    Returns:
        SystemArchiveManager: 全局唯一的存档管理器实例
    """
    global _archive_manager_instance
    
    if _archive_manager_instance is None:
        with _manager_lock:
            if _archive_manager_instance is None:
                _archive_manager_instance = SystemArchiveManager()
    
    return _archive_manager_instance


def create_system_snapshot(
    bot_service=None,
    db=None,
    custom_collectors: Optional[List[Callable]] = None
) -> Dict[str, Any]:
    """
    创建完整的系统状态快照
    
    收集所有关键组件的状态数据用于存档。
    支持通过custom_collectors参数添加自定义数据收集器。
    
    Args:
        bot_service: BotService实例（可选）
        db: 数据库管理器实例（可选）
        custom_collectors: 自定义数据收集函数列表
        
    Returns:
        dict: 系统状态快照 {
            "snapshot_time": "...",
            "components": {...},
            "version": "..."
        }
    """
    snapshot_time = datetime.now()
    snapshot = {
        "snapshot_time": snapshot_time.isoformat(),
        "snapshot_version": "2.0",
        "components": {},
        "collectors_executed": [],
        "collectors_failed": []
    }
    
    # 收集BotService状态
    if bot_service:
        collector_name = "bot_service"
        try:
            bot_state = {
                "is_running": getattr(bot_service, 'is_running', False),
                "status": bot_service.get_status() if hasattr(bot_service, 'get_status') else {},
                "browser_active": hasattr(bot_service, 'browser') and bot_service.browser is not None,
                "is_monitoring": getattr(bot_service, 'is_monitoring_messages', False),
                "current_task": getattr(bot_service, 'current_task', 'unknown')
            }
            snapshot["components"][collector_name] = bot_state
            snapshot["collectors_executed"].append(collector_name)
        except Exception as e:
            snapshot["components"][collector_name] = {"error": str(e)}
            snapshot["collectors_failed"].append(collector_name)
    
    # 收集数据库状态
    if db:
        collector_name = "database"
        try:
            customers = db.get_all_customers() if hasattr(db, 'get_all_customers') else []
            
            db_state = {
                "customer_count": len(customers),
                "intent_distribution": {},
                "status_distribution": {},
                "conversation_count": 0,
                "message_count": 0
            }
            
            for c in customers:
                level = c.get('intent_level', 'unknown')
                status = c.get('status', 'unknown')
                db_state["intent_distribution"][level] = db_state["intent_distribution"].get(level, 0) + 1
                db_state["status_distribution"][status] = db_state["status_distribution"].get(status, 0) + 1
            
            # 尝试获取会话数
            if hasattr(db, 'get_all_conversations'):
                conversations = ChatStoreFacade(db).get_all_conversations_dicts()
                db_state["conversation_count"] = len(conversations)
            
            snapshot["components"][collector_name] = db_state
            snapshot["collectors_executed"].append(collector_name)
        except Exception as e:
            snapshot["components"][collector_name] = {"error": str(e)}
            snapshot["collectors_failed"].append(collector_name)
    
    # 收集知识库状态
    collector_name = "knowledge_base"
    try:
        from src.common.unified_knowledge_service import get_unified_knowledge_service
        kb_service = get_unified_knowledge_service()
        
        if kb_service:
            kb_stats = kb_service.get_statistics() if hasattr(kb_service, 'get_statistics') else {}
            snapshot["components"][collector_name] = kb_stats
        else:
            snapshot["components"][collector_name] = {"error": "KnowledgeBase未初始化"}
        
        snapshot["collectors_executed"].append(collector_name)
    except Exception as e:
        snapshot["components"][collector_name] = {"error": str(e)}
        snapshot["collectors_failed"].append(collector_name)
    
    # 收集对话状态
    collector_name = "dialogue_manager"
    try:
        from src.common.enhanced_customer_service import get_enhanced_customer_service
        ecs = get_enhanced_customer_service()
        
        if ecs and hasattr(ecs, '_dialogue_manager'):
            dm = ecs._dialogue_manager
            session_count = len(dm.state_tracker._states) if hasattr(dm, 'state_tracker') else 0
            snapshot["components"][collector_name] = {
                "active_sessions": session_count,
                "service_initialized": True
            }
        else:
            snapshot["components"][collector_name] = {"active_sessions": 0, "service_initialized": False}
        
        snapshot["collectors_executed"].append(collector_name)
    except Exception as e:
        snapshot["components"][collector_name] = {"error": str(e)}
        snapshot["collectors_failed"].append(collector_name)
    
    # 执行自定义收集器
    if custom_collectors:
        for idx, collector in enumerate(custom_collectors):
            custom_name = f"custom_{idx}"
            try:
                result = collector()
                snapshot["components"][custom_name] = result
                snapshot["collectors_executed"].append(custom_name)
            except Exception as e:
                snapshot["components"][custom_name] = {"error": str(e)}
                snapshot["collectors_failed"].append(custom_name)
    
    # 添加收集统计
    snapshot["collection_stats"] = {
        "executed": len(snapshot["collectors_executed"]),
        "failed": len(snapshot["collectors_failed"]),
        "total_components": len(snapshot["components"]),
        "elapsed_ms": (datetime.now() - snapshot_time).total_seconds() * 1000
    }
    
    return snapshot
