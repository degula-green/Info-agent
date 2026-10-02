"""初始化MinIO bucket和生命周期策略"""

from minio import Minio
from minio.lifecycleconfig import LifecycleConfig, Rule, Expiration
import sys
from pathlib import Path
from dotenv import load_dotenv

# 加载 Agent 的 .env，和 app.main 保持同一套配置来源。否则裸跑脚本会退回
# config.py 的默认值（minio:9000），而不是部署时真正使用的 MinIO。
AGENT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(AGENT_ROOT / ".env", override=False)
sys.path.insert(0, str(AGENT_ROOT))
from app.config import settings

def init_minio():
    """创建agent-temp bucket并设置24小时过期"""

    client = Minio(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False
    )

    bucket_name = settings.attachment_minio_bucket

    # 创建bucket
    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)
        print(f"[ok] 创建bucket: {bucket_name}")
    else:
        print(f"[ok] Bucket已存在: {bucket_name}")

    # 设置生命周期：24小时后自动删除
    config = LifecycleConfig([
        Rule(
            rule_id="expire-temp-attachments",
            status="Enabled",
            expiration=Expiration(days=1)
        )
    ])
    client.set_bucket_lifecycle(bucket_name, config)
    print("[ok] 设置生命周期: 24小时自动过期")

if __name__ == "__main__":
    init_minio()
