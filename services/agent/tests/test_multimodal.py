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
    assert references_attachment("这张图片是什么内容？")
    assert references_attachment("看看附件里的照片")
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


def test_attachment_enricher_marks_image_without_text():
    mock_store = Mock()
    mock_store.get.return_value = {
        "attachment_id": "img-1",
        "owner_id": "user-1",
        "file_name": "photo.jpg",
        "mime_type": "image/jpeg",
    }
    enricher = _enricher(mock_store)

    with patch("httpx.Client") as mock_client:
        mock_response = Mock()
        mock_response.json.return_value = {
            "attachment_id": "img-1",
            "blocks": [{"type": "image", "text": "", "page_number": 1}],
            "page_count": 1,
        }
        mock_client.return_value.__enter__.return_value.post.return_value = mock_response

        result = enricher.enrich(
            {"text": "这张图片是什么内容？", "attachment_ids": ["img-1"]}
        )

    assert result.attachment_referenced is True
    # A photo with no detected text still has to tell the planner it was seen.
    assert "[图片：未识别到文字内容]" in result.enriched_text


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


def _attachment_knowledge_capabilities():
    # The real descriptors carry the input bindings; a hand-rolled descriptor
    # would hide the very conflicts this plan has to survive.
    from app.capabilities.answer import AnswerComposeCapability
    from app.capabilities.knowledge import (
        KnowledgeAnswerCapability,
        KnowledgeSearchContentCapability,
    )

    return [
        KnowledgeSearchContentCapability.descriptor,
        KnowledgeAnswerCapability.descriptor,
        AnswerComposeCapability.descriptor,
    ]


def _attachment_router():
    from app.planning.deterministic import DeterministicPlanner
    from app.planning.knowledge import KnowledgeRoutingPlanner
    from app.planning.routing import RoutingPlanner

    class FailingLLM:
        name = "llm"
        last_call_count = 0

        def create_plan(self, *args, **kwargs):
            raise AssertionError(
                "an attachment-grounded turn must not reach the LLM planner"
            )

    return KnowledgeRoutingPlanner(
        RoutingPlanner(
            deterministic=DeterministicPlanner(),
            llm=FailingLLM(),
            llm_intents=frozenset({"knowledge.answer"}),
        )
    )


def _attachment_task():
    return _task(
        {
            "text": "这张图片是什么内容？\n\n--- 附件内容 ---\n【文档：photo.jpg】\n[第1页] [图片：未识别到文字内容]",
            "_original_text": "这张图片是什么内容？",
            "_attachment_referenced": True,
            "_attachment_excerpt": "【photo.jpg】\n[第1页] [图片：未识别到文字内容]",
            "_attachment_file_names": ["photo.jpg"],
            "attachment_ids": ["img-1"],
        }
    )


def test_unjuged_attachment_turn_uses_both_sources():
    """No source verdict must default to both, never to the attachment alone."""

    plan = _attachment_router().create_plan(
        _attachment_task(),
        _attachment_knowledge_capabilities(),
        [],
        None,
        _answer_understanding(),
    )

    assert [step.capability for step in plan.steps] == [
        "knowledge.search_content",
        "answer.compose",
    ]
    answer = plan.steps[-1]
    assert answer.arguments["attachment_evidence"][0]["fetch_method"] == "attachment"
    assert answer.arguments["knowledge_evidence_refs"] == [
        {"step": 1, "output": "evidence"}
    ]


def test_attachment_only_verdict_skips_retrieval():
    """An explicit attachment-only verdict must not spend a retrieval step."""

    understanding = _answer_understanding().model_copy(
        update={"evidence_sources": ["attachment"]}
    )
    plan = _attachment_router().create_plan(
        _attachment_task(),
        _attachment_knowledge_capabilities(),
        [],
        None,
        understanding,
    )

    assert [step.capability for step in plan.steps] == ["answer.compose"]
    assert plan.steps[0].arguments["attachment_evidence"][0]["fetch_method"] == "attachment"


def test_both_source_plan_binds_inline_and_referenced_evidence():
    """Inline attachment evidence must not collide with the retrieval reference."""

    from app.kernel.bindings import bind_plan_references

    capabilities = _attachment_knowledge_capabilities()
    plan = _attachment_router().create_plan(
        _attachment_task(),
        capabilities,
        [],
        None,
        _answer_understanding(),
    )

    bound = bind_plan_references(plan, capabilities)

    answer = bound.steps[-1]
    # The reference binding fills knowledge_evidence; the attachment keeps its
    # own slot, so both sources reach the capability.
    assert answer.arguments["knowledge_evidence"] == {
        "$concat": ["$steps." + bound.steps[0].step_id + ".output.evidence"]
    }
    assert answer.arguments["attachment_evidence"][0]["fetch_method"] == "attachment"


def test_planner_answers_compliance_question_from_attachment():
    from app.kernel.models import TaskUnderstanding, UnderstandingIntent
    from app.planning.deterministic import DeterministicPlanner

    understanding = TaskUnderstanding(
        is_task=True,
        goal="判断公司是否符合独角兽定义",
        task_kind="answer",
        intent_candidates=[
            UnderstandingIntent(name="compliance.assess", confidence=0.95)
        ],
        confidence=0.95,
        reason="test",
    )
    plan = DeterministicPlanner().create_plan(
        _task(
            {
                "text": "我的公司符合“独角兽”公司的定义吗？\n\n--- 附件内容 ---\n公司成立于2023年",
                "_original_text": "我的公司符合“独角兽”公司的定义吗？",
                "_attachment_referenced": False,
                "attachment_ids": ["test-123"],
                "_attachment_excerpt": "公司成立于2023年，未引入外部融资。",
                "_attachment_file_names": ["公司简介.docx"],
            }
        ),
        _answer_capabilities(),
        [],
        None,
        understanding,
    )

    step = plan.steps[0]
    assert step.capability == "answer.compose"
    assert step.arguments["question"] == "我的公司符合“独角兽”公司的定义吗？"
    assert "未引入外部融资" in step.arguments["evidence"][0]["text"]


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
    from tests.support import TestAuthentication

    # The upload endpoint is authenticated like every other Agent route.
    app.state.agent_authentication = TestAuthentication()
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
            headers={"Authorization": "Bearer user-1-token"},
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


def test_parse_evidence_plan_maps_choice_to_sources():
    from app.understanding.laya import parse_evidence_plan

    raw = {
        "answers": {
            "evidence_plan": {
                "type": "choice",
                "choice": "attachment_and_knowledge",
                "probabilities": {
                    "attachment_only": 0.02,
                    "knowledge_only": 0.0,
                    "attachment_and_knowledge": 0.98,
                },
                "confidence": 0.98,
            }
        }
    }

    sources, confidence, probabilities = parse_evidence_plan(raw)

    assert sources == ["attachment", "knowledge"]
    assert confidence == 0.98
    assert probabilities["attachment_and_knowledge"] == 0.98


def test_parse_evidence_plan_tolerates_missing_answer():
    from app.understanding.laya import parse_evidence_plan

    assert parse_evidence_plan({"answers": {"intent": {}}}) == ([], 0.0, {})
    assert parse_evidence_plan({}) == ([], 0.0, {})


def test_jev_asks_for_sources_with_the_attachment_inventory():
    from app.understanding.laya import JevUnderstandingProvider

    class FakeClient:
        model = "fake-jev"

        def __init__(self):
            self.state = None
            self.questions = None

        def predict(self, state, questions, **kwargs):
            self.state = state
            self.questions = questions
            return {
                "model": "fake",
                "answers": {
                    "intent": {
                        "type": "choice",
                        "choice": "knowledge.answer",
                        "probabilities": {"knowledge.answer": 0.97},
                        "confidence": 0.97,
                    },
                    "evidence_plan": {
                        "type": "choice",
                        "choice": "attachment_and_knowledge",
                        "probabilities": {"attachment_and_knowledge": 0.95},
                        "confidence": 0.95,
                    },
                },
            }

    client = FakeClient()
    provider = JevUnderstandingProvider(client, min_confidence=0.9)
    task = _task(
        {
            "text": "简要介绍我的公司",
            "attachment_ids": ["a1"],
            "_attachment_file_names": ["公司简介.docx"],
            "_attachment_excerpt": "公司成立于2020年",
        }
    )

    result = provider.understand(task)

    assert "evidence_plan" in client.questions
    assert client.state["attachments"] == [
        {"name": "公司简介.docx", "kind": "document", "has_text": True}
    ]
    assert result.evidence_sources == ["attachment", "knowledge"]


def test_laya_does_not_ask_for_sources():
    """The fine-tuned local checkpoint keeps its frozen single question."""

    from app.understanding.laya import LayaUnderstandingProvider

    class FakeClient:
        model = "fake-laya"

        def __init__(self):
            self.state = None
            self.questions = None

        def predict(self, state, questions, **kwargs):
            self.state = state
            self.questions = questions
            return {
                "answers": {
                    "intent": {
                        "type": "choice",
                        "choice": "knowledge.answer",
                        "probabilities": {"knowledge.answer": 0.97},
                        "confidence": 0.97,
                    }
                }
            }

    client = FakeClient()
    provider = LayaUnderstandingProvider(client, min_confidence=0.9)
    task = _task(
        {
            "text": "简要介绍我的公司",
            "attachment_ids": ["a1"],
            "_attachment_file_names": ["公司简介.docx"],
            "_attachment_excerpt": "公司成立于2020年",
        }
    )

    result = provider.understand(task)

    assert "evidence_plan" not in client.questions
    assert "attachments" not in client.state
    assert result.evidence_sources == []


def test_hybrid_keeps_primary_sources_when_intent_falls_back():
    from app.kernel.models import TaskUnderstanding
    from app.understanding.hybrid import HybridUnderstandingProvider
    from app.understanding.laya import LayaEvaluation

    class Primary:
        name = "jev"
        model = "jev"
        request_evidence_plan = True
        last_call_count = 1

        def evaluate(self, task, *, min_confidence=None):
            return LayaEvaluation(
                understanding=TaskUnderstanding(
                    is_task=True,
                    goal="x",
                    confidence=0.2,
                    evidence_sources=["attachment", "knowledge"],
                    evidence_reason="jev: attachment+knowledge",
                ),
                label="knowledge.answer",
                answer_confidence=0.2,
                margin=0.0,
                probabilities={"knowledge.answer": 0.4},
                accepted=False,
                fallback_reason="low_probability",
            )

    class Fallback:
        name = "llm"
        model = "llm"
        last_call_count = 1

        def understand(self, task, *, min_confidence=None, conversation_context=None):
            return TaskUnderstanding(is_task=True, goal="x", confidence=0.9)

    provider = HybridUnderstandingProvider(primary=Primary(), fallback=Fallback())

    result = provider.understand(_task({"text": "简要介绍我的公司"}))

    assert provider.last_decision_source == "llm"
    assert result.evidence_sources == ["attachment", "knowledge"]


def test_content_merge_drops_chunks_below_the_floor():
    from app.capabilities.knowledge import _merge_content

    responses = [
        {
            "items": [
                {
                    "resource_id": "r1",
                    "resource_type": "message",
                    "best_score": 0.016,
                    "chunks": [
                        {"chunk_id": "keep", "text": "相关片段", "score": 0.016},
                        {"chunk_id": "drop", "text": "无关片段", "score": 0.002},
                    ],
                }
            ]
        }
    ]

    results = _merge_content(responses, limit=10, min_score=0.005)

    assert [chunk.chunk_id for chunk in results[0].chunks] == ["keep"]


def test_content_merge_skips_a_resource_with_no_surviving_chunk():
    from app.capabilities.knowledge import _merge_content

    responses = [
        {
            "items": [
                {
                    "resource_id": "r1",
                    "resource_type": "message",
                    "best_score": 0.002,
                    "chunks": [
                        {"chunk_id": "drop", "text": "无关片段", "score": 0.002}
                    ],
                }
            ]
        }
    ]

    assert _merge_content(responses, limit=10, min_score=0.005) == []


def test_web_research_searches_the_instruction_not_the_attachment_body():
    """The merged attachment body must never become the search query."""

    from app.kernel.models import CapabilityDescriptor, UnderstandingIntent
    from app.kernel.models import TaskUnderstanding
    from app.planning.deterministic import DeterministicPlanner
    from app.planning.routing import RoutingPlanner

    class NeverPlanner:
        name = "never"
        last_call_count = 0

        def create_plan(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("the public-web pipeline is deterministic")

    capabilities = [
        CapabilityDescriptor(
            name="web.research",
            description="web.research",
            risk_level="read_only",
            side_effect=False,
            requires_approval=False,
            idempotent=True,
            timeout_seconds=60,
        )
    ]
    planner = RoutingPlanner(
        deterministic=DeterministicPlanner(),
        llm=NeverPlanner(),
    )
    body = "第1页 政策全文" * 400
    task = _task(
        {
            "text": f"搜一下这个政策的最新版本\n\n--- 附件内容 ---\n【政策.pdf】\n{body}",
            "_original_text": "搜一下这个政策的最新版本",
            "attachment_ids": ["a1"],
            "_attachment_file_names": ["政策.pdf"],
            "_attachment_excerpt": body,
        }
    )
    understanding = TaskUnderstanding(
        is_task=True,
        goal="搜索政策",
        task_kind="action",
        intent_candidates=[
            UnderstandingIntent(name="web.research", confidence=0.95)
        ],
        confidence=0.95,
    )

    plan = planner.create_plan(task, capabilities, [], None, understanding)

    step = plan.steps[0]
    assert step.capability == "web.research"
    assert step.arguments["queries"] == ["搜一下这个政策的最新版本"]
    assert "附件内容" not in str(step.arguments)


def _parse_error(status: int):
    import httpx

    request = httpx.Request("POST", "http://rag-service:8000/internal/v1/parse-attachment")
    return httpx.HTTPStatusError(
        f"server error '{status}'",
        request=request,
        response=httpx.Response(status, request=request),
    )


def _enricher_with_store() -> tuple:
    from app.kernel.attachment_enricher import AttachmentContextEnricher

    mock_store = Mock()
    mock_store.get.return_value = {
        "attachment_id": "a1",
        "owner_id": "user-1",
        "file_name": "meeting.pdf",
        "mime_type": "application/pdf",
    }
    enricher = AttachmentContextEnricher(
        rag_service_url="http://rag-service:8000",
        rag_internal_token="test-token",
        attachment_store=mock_store,
    )
    return enricher, mock_store


def test_attachment_parse_retries_a_transient_server_error():
    """A RAG restart mid-request must not discard an otherwise fine attachment."""

    from unittest.mock import patch

    enricher, _ = _enricher_with_store()

    class Client:
        calls = 0

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            Client.calls += 1
            if Client.calls < 3:
                raise _parse_error(500)
            response = Mock()
            response.raise_for_status = lambda: None
            response.json.return_value = {
                "blocks": [{"type": "text", "text": "会议时间：明天下午2点", "page_number": 1}],
                "page_count": 1,
            }
            return response

    with patch("httpx.Client", Client), patch(
        "app.kernel.attachment_enricher.time.sleep", lambda *_: None
    ):
        result = enricher.enrich({"text": "根据这个附件创建日程", "attachment_ids": ["a1"]})

    assert Client.calls == 3
    assert "明天下午2点" in result.enriched_text


def test_attachment_parse_does_not_retry_a_client_error():
    """A missing object is a verdict, not a blip."""

    from unittest.mock import patch

    enricher, _ = _enricher_with_store()

    class Client:
        calls = 0

        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            Client.calls += 1
            raise _parse_error(404)

    with patch("httpx.Client", Client), patch(
        "app.kernel.attachment_enricher.time.sleep", lambda *_: None
    ):
        result = enricher.enrich({"text": "根据这个附件创建日程", "attachment_ids": ["a1"]})

    assert Client.calls == 1
    assert "文档解析失败" in result.enriched_text


def test_failed_parse_still_exports_an_attachment_excerpt():
    """A broken file must stay the turn's evidence, not fall back to retrieval."""

    from unittest.mock import patch

    enricher, _ = _enricher_with_store()

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            raise _parse_error(404)

    with patch("httpx.Client", Client), patch(
        "app.kernel.attachment_enricher.time.sleep", lambda *_: None
    ):
        result = enricher.enrich(
            {"text": "简要介绍我的公司", "attachment_ids": ["a1"]}
        )

    assert "文档解析失败" in result.attachment_excerpt


def test_failed_parse_keeps_the_attachment_answer_path():
    """The planner answers from the failure note instead of searching elsewhere."""

    task = _task(
        {
            "text": "简要介绍我的公司",
            "_original_text": "简要介绍我的公司",
            "_attachment_referenced": True,
            "_attachment_excerpt": "【company-profile.pdf】\n[文档解析失败: 服务暂时不可用]",
            "_attachment_file_names": ["company-profile.pdf"],
            "attachment_ids": ["a1"],
        }
    )
    understanding = _answer_understanding().model_copy(
        update={"evidence_sources": ["attachment"]}
    )

    plan = _attachment_router().create_plan(
        task,
        _attachment_knowledge_capabilities(),
        [],
        None,
        understanding,
    )

    assert [step.capability for step in plan.steps] == ["answer.compose"]
    assert "文档解析失败" in plan.steps[0].arguments["attachment_evidence"][0]["text"]


def test_referenced_attachment_survives_a_knowledge_only_verdict():
    """The model may add sources; it may not drop the file the user pointed at."""

    from app.planning.routing import resolve_evidence_sources

    task = _attachment_task()
    understanding = _answer_understanding().model_copy(
        update={"evidence_sources": ["knowledge"]}
    )

    assert resolve_evidence_sources(task, understanding) == ("attachment",)


def test_unjuged_referenced_attachment_still_uses_both_sources():
    from app.planning.routing import resolve_evidence_sources

    assert resolve_evidence_sources(_attachment_task(), None) == (
        "attachment",
        "knowledge",
    )


def test_attachment_without_reference_keeps_the_judged_sources():
    """A stray file must not force the attachment path."""

    from app.planning.routing import resolve_evidence_sources

    task = _task(
        {
            "text": "公司办公地址是什么",
            "attachment_ids": ["a1"],
            "_attachment_referenced": False,
            "_attachment_excerpt": "无关内容",
        }
    )
    understanding = _answer_understanding().model_copy(
        update={"evidence_sources": ["knowledge"]}
    )

    assert resolve_evidence_sources(task, understanding) == ("knowledge",)
