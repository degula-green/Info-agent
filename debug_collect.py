from services.collectors.wechat import service
import hashlib, json, urllib.error

chat = "44505237054@chatroom"
collector_id = "87f9a34a-6ccf-4c8f-966e-7735830ed1e1"
rows = service.messages_after(service.db, chat, 1789396951000, 200)
for raw in rows:
    local_id = str(raw.get("local_id") or raw.get("server_id") or raw.get("sort_seq") or "0")
    attachments = service.attachment_metadata(chat, raw)
    sender_external_id, sender_display_name = service.resolve_sender(raw, service.nickname_index())
    content = service.strip_sender_prefix(raw.get("content"), sender_external_id)
    value = {
        "collector_id": collector_id,
        "external_conversation_id": chat,
        "external_message_id": f"{service.binding.get('wxid', '')}:{chat}:{local_id}",
        "sender_external_id": sender_external_id,
        "sender_display_name": sender_display_name,
        "message_type": service.normalized_type(raw),
        "content": content,
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "sent_at": service.parse_time(raw.get("create_time")).isoformat().replace("+00:00", "Z"),
        "cursor": str(raw.get("sort_seq") or raw.get("local_id") or ""),
        "attachments": attachments,
    }
    value["payload_hash"] = service.payload_hash(value)
    print("TRY", local_id, value["message_type"], [a["file_name"] for a in attachments])
    print("HASH", value["content_hash"], "SENDER", repr(sender_external_id), repr(sender_display_name))
    try:
        result = service.knowledge(f"/api/knowledge/v1/internal/collectors/{collector_id}/messages", "POST", value)
        print("OK", result)
    except urllib.error.HTTPError as exc:
        print("HTTP", exc.code, exc.read().decode())
