"""多模态附件：存储、增强、条件取材与上传接口。"""

from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import Mock, patch


def test_attachment_store():
    """测试附件存储"""
    from app.infrastructure.attachment_store import RedisAttachmentStore, AttachmentMetadata

    mock_redis = Mock()
    store = RedisAttachmentStore(mock_redis, ttl_hours=24)
    metadata = AttachmentMetadata(
        attachment_id="test-123",
        owner_id="user-1",
        file_name="test.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        minio_object_name="user-1/test-123.pdf",
        uploaded_at=datetime.now(timezone.utc).isoformat(),
        expires_at=datetime.now(timezone.utc).isoformat(),
    )

    store.save(metadata)

    assert mock_redis.setex.called
    call_args = mock_redis.setex.call_args
    assert call_args[0][0] == "attachment:test-123"
    assert call_args[0][1] == 24 * 3600


def test_references_attachment():
    from app.kernel.attachment_enricher import references_attachment

    assert references_attachment("根据这个附件创建日程")
    assert references_attachment("按照文档里的时间建一个提醒")
    assert references_attachment("从文件里提取会议信息")
    assert references_attachment("参考上述内容安排一下")
    assert not references_attachment("明天下午三点跟张三开评审会")
    assert not references_attachment("整理一下我的待办")


def _parsed_document(block_count: int = 12) -> dict:
    blocks = [
        {
            "type": "title" if index == 0 else "text",
            "text": f"第{index + 1}段内容",
            "page_number": (index // 4) + 1,
            "searchable_text": f"第{index + 1}段内容",
            "heading_path": ["会议纪要"] if index else [],
            "level": 1 if index == 0 else None,
        }
        for index in range(block_count)
    ]
    return {"attachment_id": "test-123", "blocks": blocks, "page_count": 3}


def _enricher(store: Mock):
    from app.kernel.attachment_enricher import AttachmentContextEnricher

    return AttachmentContextEnricher(
        rag_service_url="http://rag-service:8000",
        rag_internal_token="test-token",
        attachment_store=store,
    )


def test_attachment_enricher_referenced_uses_full_body():
    mock_store = Mock()
    mock_store.get.return_value = {
        "attachment_id": "test-123",
        "owner_id": "user-1",
        "file_name": "meeting.pdf",
        "mime_type": "application/pdf",
    }
    enricher = _enricher(mock_store)

    with patch("httpx.Client") as mock_client:
        mock_response = Mock()
        mock_response.json.return_value = _parsed_document(12)
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = enricher.enrich(
            {"text": "根据这个附件创建日程", "attachment_ids": ["test-123"]}
        )

    assert result.original_text == "根据这个附件创建日程"
    assert result.attachment_referenced is True
    assert "meeting.pdf" in result.enriched_text
    # 引用附件时注入全文，而不是只取前 10 个 block。
    assert "第12段内容" in result.enriched_text
    assert "第12段内容" in result.attachment_excerpt


def test_attachment_enricher_not_referenced_keeps_original_rules():
    mock_store = Mock()
    mock_store.get.return_value = {
        "attachment_id": "test-123",
        "owner_id": "user-1",
        "file_name": "meeting.pdf",
        "mime_type": "application/pdf",
    }
    enricher = _enricher(mock_store)

    with patch("httpx.Client") as mock_client:
        mock_response = Mock()
        mock_response.json.return_value = _parsed_document(12)
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = enricher.enrich(
            {"text": "明天下午三点跟张三开评审会", "attachment_ids": ["test-123"]}
        )

    assert result.attachment_referenced is False
    assert "meeting.pdf" in result.enriched_text


def test_hybrid_understanding_prefers_llm_for_attachment_reference():
    from app.kernel.models import TaskUnderstanding, UnderstandingIntent
    from app.understanding.hybrid import HybridUnderstandingProvider

    class Laya:
        name = "laya"
        model = "laya"
        estimated_model_calls = 1
        last_call_count = 0

        def evaluate(self, task, *, min_confidence=None):
            raise AssertionError("Laya must not run for attachment-referenced input")

        def understand(self, task, *, min_confidence=None):
            raise AssertionError("Laya must not run for attachment-referenced input")

    class Fallback:
        name = "llm"
        model = "fake-llm"
        last_call_count = 1

        def understand(self, task, *, min_confidence=None):
            return TaskUnderstanding(
                is_task=True,
                goal="创建日程",
                task_kind="action",
                intent_candidates=[
                    UnderstandingIntent(name="todo.create", confidence=0.95)
                ],
                confidence=0.95,
                reason="llm",
            )

    provider = HybridUnderstandingProvider(laya=Laya(), fallback=Fallback())
    result = provider.understand(
        _task({"text": "根据这个附件创建日程", "attachment_ids": ["a1"]})
    )

    assert result.intent_candidates[0].name == "todo.create"
    assert provider.last_decision_source == "llm"
    assert provider.last_fallback_reason == "attachment_reference"


def _todo_capabilities():
    from app.kernel.models import CapabilityDescriptor

    return [
        CapabilityDescriptor(
            name="todo.create",
            description="创建一条待办",
            risk_level="external_write",
            side_effect=True,
            requires_approval=True,
            idempotent=True,
            timeout_seconds=10,
        )
    ]


def _todo_understanding():
    from app.kernel.models import TaskUnderstanding, UnderstandingIntent

    return TaskUnderstanding(
        is_task=True,
        goal="创建日程",
        task_kind="action",
        intent_candidates=[UnderstandingIntent(name="todo.create", confidence=0.95)],
        confidence=0.95,
        reason="test",
    )


def _task(input_: dict):
    from app.kernel.models import TaskEnvelope

    return TaskEnvelope(
        task_id="task-test",
        source_type="chat",
        owner_user_id="user-1",
        input=input_,
        created_at=datetime.now(timezone.utc),
    )


def _plan(input_: dict):
    from app.planning.deterministic import DeterministicPlanner

    planner = DeterministicPlanner()
    return planner.create_plan(
        _task(input_),
        _todo_capabilities(),
        [],
        None,
        _todo_understanding(),
    )


def test_planner_ignores_attachment_when_not_referenced():
    plan = _plan(
        {
            "text": "明天下午三点跟张三开评审会\n\n--- 附件内容 ---\n【文档：notes.pdf】[第1页] 明年一月搬办公室",
            "_original_text": "明天下午三点跟张三开评审会",
            "_attachment_referenced": False,
            "attachment_ids": ["test-123"],
            "_attachment_excerpt": "明年一月搬办公室",
        }
    )

    arguments = plan.steps[0].arguments
    assert arguments["title"] == "跟张三开评审会"
    assert arguments["due_expression"] == "明天下午三点"
    assert "notes" not in arguments


def test_planner_uses_attachment_when_referenced():
    plan = _plan(
        {
            "text": "根据这个附件创建日程\n\n--- 附件内容 ---\n【文档：meeting.pdf】[第1页] 会议时间：明天下午2点-4点",
            "_original_text": "根据这个附件创建日程",
            "_attachment_referenced": True,
            "attachment_ids": ["test-123"],
            "_attachment_excerpt": "会议时间：明天下午2点-4点\n会议地点：3楼会议室",
        }
    )

    arguments = plan.steps[0].arguments
    assert arguments["title"] == "根据这个附件创建日程"
    assert arguments["due_expression"] == "明天下午2点"
    assert "3楼会议室" in arguments["notes"]


def _answer_capabilities():
    from app.kernel.models import CapabilityDescriptor

    return [
        CapabilityDescriptor(
            name="answer.compose",
            description="依据 evidence 组织回答",
            risk_level="read_only",
            side_effect=False,
            requires_approval=False,
            idempotent=True,
            timeout_seconds=60,
        )
    ]


def _answer_understanding():
    from app.kernel.models import TaskUnderstanding, UnderstandingIntent

    return TaskUnderstanding(
        is_task=True,
        goal="回答附件问题",
        task_kind="answer",
        intent_candidates=[
            UnderstandingIntent(name="knowledge.answer", confidence=0.95)
        ],
        confidence=0.95,
        reason="test",
    )


def test_planner_answers_document_question_from_attachment():
    from app.planning.deterministic import DeterministicPlanner

    plan = DeterministicPlanner().create_plan(
        _task(
            {
                "text": "这个文档内容大致是什么？\n\n--- 附件内容 ---\n【文档：常见问题.docx】\n青云飞鹏小组属于学术科技类社团",
                "_original_text": "这个文档内容大致是什么？",
                "_attachment_referenced": True,
                "attachment_ids": ["test-123"],
                "_attachment_excerpt": "【常见问题.docx】\n青云飞鹏小组属于学术科技类社团，专注于计算机技术学习。",
                "_attachment_file_names": ["常见问题.docx"],
            }
        ),
        _answer_capabilities(),
        [],
        None,
        _answer_understanding(),
    )

    step = plan.steps[0]
    assert step.capability == "answer.compose"
    assert step.arguments["question"] == "这个文档内容大致是什么？"
    assert "青云飞鹏" in step.arguments["evidence"][0]["text"]
    assert step.arguments["evidence"][0]["title"] == "常见问题.docx"


def test_planner_keeps_document_question_unsupported_without_attachment():
    from app.planning.deterministic import DeterministicPlanner

    plan = DeterministicPlanner().create_plan(
        _task({"text": "这个文档内容大致是什么？"}),
        _answer_capabilities(),
        [],
        None,
        _answer_understanding(),
    )

    assert plan.steps == []
    assert plan.unsupported_intents == ["knowledge.answer"]


def test_upload_attachment_api():
    """测试附件上传API（MinIO/Redis 都以依赖覆盖注入）"""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routers import attachments

    client = TestClient(app)
    test_file = BytesIO(b"test pdf content")
    mock_minio = Mock()
    mock_store = Mock()

    app.dependency_overrides[attachments.get_minio_client] = lambda: mock_minio
    app.dependency_overrides[attachments.get_attachment_store] = lambda: mock_store
    try:
        response = client.post(
            "/api/agent/v1/attachments",
            files={"file": ("test.pdf", test_file, "application/pdf")},
            headers={"x-agent-user-id": "test-user"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["attachment_id"]
    assert data["file_name"] == "test.pdf"
    assert data["mime_type"] == "application/pdf"
    assert mock_minio.put_object.called
    assert mock_store.save.called


def test_create_task_with_attachment():
    """测试创建带附件的任务"""
    from app.routers.tasks import CreateTaskBody

    body = CreateTaskBody(
        text="根据这个附件创建日程",
        attachment_ids=["test-123"],
    )

    assert body.text == "根据这个附件创建日程"
    assert body.attachment_ids == ["test-123"]
