from __future__ import annotations

import asyncio
import unittest
from dataclasses import replace

from app.config import settings
from app.dependencies import get_retrieval_service
from app.main import app


class Phase1AContractTests(unittest.TestCase):
    def test_production_requires_authorization_configuration(self) -> None:
        configured = replace(
            settings,
            environment="production",
            authz_base_url="",
            authz_api_token="",
        )
        with self.assertRaisesRegex(RuntimeError, "RAG_AUTHZ_BASE_URL"):
            configured.validate_mvp()

    def test_development_allows_explicit_allow_all_fallback(self) -> None:
        configured = replace(
            settings,
            environment="development",
            authz_base_url="",
            authz_api_token="",
        )
        configured.validate_mvp()

    def test_lifespan_reuses_single_application_container(self) -> None:
        async def run() -> None:
            async with app.router.lifespan_context(app):
                container = app.state.container
                first = get_retrieval_service(container=container)
                second = get_retrieval_service(container=container)
                self.assertIs(first, second)
                self.assertIs(first.repository, container.repository)
                self.assertIs(first.authorization, container.authorization)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
