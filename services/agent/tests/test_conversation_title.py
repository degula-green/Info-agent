from __future__ import annotations

from datetime import datetime, timezone

from app.application.conversation_title import ConversationTitleService
from app.kernel.models import ConversationRecord, MessageRecord, TaskRecord
from app.testing.in_memory_runtime_store import InMemoryAgentStore
from tests.support import make_settings


class _TitleProvider:
    def __init__(self, title: str = "青云官网部署进度") -> None:
        self.title = title
        self.calls = 0

    def generate(self, messages, *, max_chars: int) -> str:
        self.calls += 1
        assert messages
        assert messages[0].role == "user"
        assert len(messages) in {1, 2}
        return self.title


def _seed_turn(
    *,
    title: str = "青云官网当前在哪个阶段了？",
    include_assistant: bool = True,
    task_status: str = "succeeded",
) -> tuple[InMemoryAgentStore, str]:
    store = InMemoryAgentStore()
    moment = datetime.now(timezone.utc)
    conversation_id = "00000000-0000-0000-0000-000000000001"
    task_id = "00000000-0000-0000-0000-000000000010"
    user = MessageRecord(
        message_id="00000000-0000-0000-0000-000000000011",
        conversation_id=conversation_id,
        role="user",
        content="青云官网当前在哪个阶段了？",
        status="completed",
        task_id=task_id,
        created_at=moment,
        updated_at=moment,
    )
    assistant = (
        MessageRecord(
            message_id="00000000-0000-0000-0000-000000000012",
            conversation_id=conversation_id,
            role="assistant",
            content="本地知识显示青云官网已部署到 116 服务器。",
            status="completed",
            task_id=task_id,
            created_at=moment,
            updated_at=moment,
        )
        if include_assistant
        else None
    )
    task = TaskRecord(
        task_id=task_id,
        source_type="chat",
        owner_user_id="user-1",
        status=task_status,
        input={"text": user.content},
        conversation_id=conversation_id,
        response_message_id=assistant.message_id if assistant else None,
        created_at=moment,
        updated_at=moment,
    )
    store.create_conversation(
        ConversationRecord(
            conversation_id=conversation_id,
            owner_user_id="user-1",
            title=title,
            created_at=moment,
            updated_at=moment,
        )
    )
    store.create_task(task)
    store.add_message(user)
    if assistant is not None:
        store.add_message(assistant)
    return store, task_id


def test_generates_title_after_first_completed_turn() -> None:
    store, task_id = _seed_turn()
    provider = _TitleProvider()
    service = ConversationTitleService(
        store,
        make_settings(
            conversation_title_enabled=True,
            conversation_title_max_chars=20,
        ),
        provider,
    )

    assert service.maybe_generate_after_task(task_id) == "青云官网部署进度"
    assert provider.calls == 1
    conversation = store.get_conversation("00000000-0000-0000-0000-000000000001")
    assert conversation is not None
    assert conversation.title == "青云官网部署进度"


def test_manual_title_is_not_overwritten() -> None:
    store, task_id = _seed_turn(title="手动标题")
    provider = _TitleProvider()
    service = ConversationTitleService(
        store,
        make_settings(conversation_title_enabled=True),
        provider,
    )

    assert service.maybe_generate_after_task(task_id) is None
    assert provider.calls == 0
    conversation = store.get_conversation("00000000-0000-0000-0000-000000000001")
    assert conversation is not None
    assert conversation.title == "手动标题"


def test_failed_task_without_answer_still_gets_a_title() -> None:
    store, task_id = _seed_turn(
        include_assistant=False,
        task_status="failed",
    )
    provider = _TitleProvider("数据库配置在哪里")
    service = ConversationTitleService(
        store,
        make_settings(conversation_title_enabled=True),
        provider,
    )

    assert service.maybe_generate_after_task(task_id) == "数据库配置在哪里"
    conversation = store.get_conversation("00000000-0000-0000-0000-000000000001")
    assert conversation is not None
    assert conversation.title == "数据库配置在哪里"
