"""签发一个本地测试用的访问令牌（RS256）。

    python scripts/make-test-token.py <jwt-private.pem> <user_id> [--ttl 900]

需要 PyJWT + cryptography，用装了这两个包的虚拟环境跑，例如：

    services/agent/.venv/Scripts/python.exe scripts/make-test-token.py \
        services/core/.runtime/jwt-private.pem <user_id>

iss / aud / kid 跟随 core 的约定，密钥来自 core 的 .runtime。仅用于本地调试。
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from jwt import encode

ISSUER = "info-agent-core"
AUDIENCE = "info-agent-api"
KEY_ID = "v1"
DEFAULT_TTL_SECONDS = 900


def main() -> int:
    parser = argparse.ArgumentParser(description="签发本地调试用的访问令牌")
    parser.add_argument("key_path", type=Path, help="core 的 RS256 私钥（PEM）")
    parser.add_argument("user_id", help="签发对象（写入 sub）")
    parser.add_argument(
        "--ttl", type=int, default=DEFAULT_TTL_SECONDS, help="有效期秒数，默认 900"
    )
    args = parser.parse_args()

    if not args.key_path.is_file():
        print(f"私钥不存在: {args.key_path}", file=sys.stderr)
        return 1

    key = serialization.load_pem_private_key(args.key_path.read_bytes(), password=None)
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": args.user_id,
        "sid": str(uuid.uuid4()),
        "typ": "access",
        "auth_time": now,
        "iat": now,
        "nbf": now,
        "exp": now + max(1, args.ttl),
        "jti": str(uuid.uuid4()),
    }
    print(encode(payload, key, algorithm="RS256", headers={"kid": KEY_ID}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
