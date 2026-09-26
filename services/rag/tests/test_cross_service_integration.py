from __future__ import annotations

import json
import os
import time
import unittest
import urllib.request
import urllib.error
import uuid

import psycopg

from app.config import settings
from app.infrastructure.rag_elasticsearch import RagChunkIndex


@unittest.skipUnless(
    os.getenv("RAG_CROSS_SERVICE_INTEGRATION") == "1",
    "cross-service integration disabled",
)
class CrossServiceIntegrationTests(unittest.TestCase):
    def test_collector_fixture_to_rag_search(self) -> None:
        ids = {name: str(uuid.uuid4()) for name in (
            "base", "connector", "conversation", "collector", "owner",
        )}
        item_ids: list[str] = []
        index = RagChunkIndex()
        with psycopg.connect(settings.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO knowledge.knowledge_bases
                       (id,knowledge_scope,base_type,name,owner_user_id,source_key,status)
                       VALUES (%s::uuid,'private','private_conversation','integration private',%s::uuid,%s,'active')""",
                    (ids["base"], ids["owner"], ids["conversation"]),
                )
                cursor.execute(
                    """INSERT INTO knowledge.connector_accounts
                       (id,owner_user_id,platform,platform_workspace_key,external_account_id,
                        display_name,credential_ref,status)
                       VALUES (%s::uuid,%s::uuid,'wechat','',%s,'integration','test','active')""",
                    (ids["connector"], ids["owner"], f"integration-{ids['connector']}"),
                )
                cursor.execute(
                    """INSERT INTO knowledge.conversation_ingestions
                       (id,platform,platform_workspace_key,external_conversation_id,conversation_type,
                        name,knowledge_base_id,ingestion_scope,owner_user_id,created_by_user_id,
                        requested_start_at,effective_start_at,status)
                       VALUES (%s::uuid,'wechat','',%s,'private','integration private',%s::uuid,
                               'private',%s::uuid,%s::uuid,'2026-09-01T00:00:00Z',
                               '2026-09-01T00:00:00Z','active')""",
                    (
                        ids["conversation"], f"integration-{ids['conversation']}",
                        ids["base"], ids["owner"], ids["owner"],
                    ),
                )
                cursor.execute(
                    """INSERT INTO knowledge.conversation_collectors
                       (id,conversation_ingestion_id,connector_account_id,collector_user_id,
                        collector_role,status)
                       VALUES (%s::uuid,%s::uuid,%s::uuid,%s::uuid,'primary','active')""",
                    (
                        ids["collector"], ids["conversation"], ids["connector"], ids["owner"],
                    ),
                )
            connection.commit()
        try:
            _post_json(
                f"{settings.knowledge_base_url}/v1/internal/fixtures/replay",
                {
                    "conversation_id": ids["conversation"],
                    "collector_id": ids["collector"],
                    "start_at": "2026-09-01T00:00:00Z",
                    "end_at": "2026-09-30T00:00:00Z",
                    "messages": [{
                        "external_id": f"integration-{ids['conversation']}",
                        "sender_external_id": f"owner-{ids['owner']}",
                        "sender_name": "Integration Owner",
                        "message_type": "text",
                        "content": "pipeline marker for rag mvp",
                        "sent_at": "2026-09-27T00:00:00Z",
                        "cursor": "1",
                    }],
                    "pages": [{"cursor": "1", "messages": [{
                        "external_id": f"integration-{ids['conversation']}",
                        "sender_external_id": f"owner-{ids['owner']}",
                        "sender_name": "Integration Owner",
                        "message_type": "text",
                        "content": "pipeline marker for rag mvp",
                        "sent_at": "2026-09-27T00:00:00Z",
                        "cursor": "1",
                    }]}],
                },
                token=settings.knowledge_api_token,
            )
            deadline = time.time() + 60
            status = ""
            while time.time() < deadline:
                with psycopg.connect(settings.database_url) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """SELECT ki.id::text,COALESCE(ki.rag_status,'pending'),COALESCE(ki.rag_last_error,'')
                               FROM knowledge.knowledge_items ki
                               WHERE ki.conversation_ingestion_id=%s::uuid
                                 AND ki.source_message_id IS NOT NULL""",
                            (ids["conversation"],),
                        )
                        row = cursor.fetchone()
                if row:
                    item_ids = [row[0]]
                    status = row[1]
                    if status in {"ready", "metadata_only", "failed"}:
                        break
                time.sleep(1)
            if status != "ready":
                diagnostic = {}
                with psycopg.connect(settings.database_url) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """SELECT id::text,status,current_stage,COALESCE(last_error,'')
                               FROM rag_mvp.processing_jobs
                               WHERE knowledge_item_id=ANY(%s::uuid[])
                               ORDER BY created_at DESC LIMIT 1""",
                            (item_ids,),
                        )
                        job = cursor.fetchone()
                        diagnostic["job"] = job
                        if job:
                            cursor.execute(
                                """SELECT lane,stage,status,COALESCE(error_code,''),
                                          COALESCE(error_message,'')
                                   FROM rag_mvp.processing_job_attempts
                                   WHERE job_id=%s::uuid ORDER BY created_at DESC LIMIT 5""",
                                (job[0],),
                            )
                            diagnostic["attempts"] = cursor.fetchall()
                self.fail(f"rag status was {status}: {diagnostic}")
            response = _post_json(
                "http://127.0.0.1:8000/api/v1/search/global",
                {
                    "query": "pipeline marker",
                    "scope_type": "user",
                    "top_k": 5,
                    "include_protected": False,
                },
                token=None,
                headers={"X-User-ID": ids["owner"]},
            )
            self.assertTrue(any("pipeline marker" in str(item.get("content", "")) for item in response.get("items", [])))
        finally:
            if item_ids:
                index.client.delete_by_query(
                    index="rag_chunks_display_write",
                    query={"terms": {"knowledge_item_id": item_ids}},
                    conflicts="proceed",
                    refresh=True,
                )
                with psycopg.connect(settings.database_url) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "DELETE FROM rag_mvp.outbox_events WHERE job_id IN "
                            "(SELECT id FROM rag_mvp.processing_jobs WHERE knowledge_item_id=ANY(%s::uuid[]))",
                            (item_ids,),
                        )
                        cursor.execute(
                            "DELETE FROM rag_mvp.processing_jobs WHERE knowledge_item_id=ANY(%s::uuid[])",
                            (item_ids,),
                        )
                        cursor.execute(
                            "DELETE FROM rag_mvp.resource_snapshots WHERE knowledge_item_id=ANY(%s::uuid[])",
                            (item_ids,),
                        )
            with psycopg.connect(settings.database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM knowledge.outbox_events WHERE aggregate_id IN "
                        "(SELECT id FROM knowledge.knowledge_items WHERE conversation_ingestion_id=%s::uuid)",
                        (ids["conversation"],),
                    )
                    cursor.execute(
                        "DELETE FROM knowledge.knowledge_items WHERE conversation_ingestion_id=%s::uuid",
                        (ids["conversation"],),
                    )
                    cursor.execute(
                        "DELETE FROM knowledge.message_sources WHERE message_id IN "
                        "(SELECT id FROM knowledge.messages WHERE conversation_ingestion_id=%s::uuid)",
                        (ids["conversation"],),
                    )
                    cursor.execute(
                        "DELETE FROM knowledge.messages WHERE conversation_ingestion_id=%s::uuid",
                        (ids["conversation"],),
                    )
                    cursor.execute(
                        "DELETE FROM knowledge.collector_cursor_receipts WHERE collector_id=%s::uuid",
                        (ids["collector"],),
                    )
                    cursor.execute(
                        "DELETE FROM knowledge.conversation_collectors WHERE conversation_ingestion_id=%s::uuid",
                        (ids["conversation"],),
                    )
                    cursor.execute(
                        "DELETE FROM knowledge.conversation_ingestions WHERE id=%s::uuid",
                        (ids["conversation"],),
                    )
                    cursor.execute(
                        "DELETE FROM knowledge.connector_accounts WHERE id=%s::uuid",
                        (ids["connector"],),
                    )
                    cursor.execute(
                        "DELETE FROM knowledge.knowledge_bases WHERE id=%s::uuid",
                        (ids["base"],),
                    )
                connection.commit()


def _post_json(
    url: str,
    body: dict,
    *,
    token: str | None,
    headers: dict[str, str] | None = None,
) -> dict:
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise AssertionError(f"{exc.code} from {url}: {body}") from exc


if __name__ == "__main__":
    unittest.main()
