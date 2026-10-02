"""附件上下文增强器：在Understanding之前解析附件并注入上下文"""

from typing import Any
import httpx
from dataclasses import dataclass, field
import logging

from app.ingress.vocabulary import references_attachment

logger = logging.getLogger("agent.attachment")

@dataclass
class EnrichedContext:
    """增强后的上下文"""
    original_text: str
    enriched_text: str
    attachment_metadata: list[dict] = field(default_factory=list)
    # True only when the user's own sentence pointed at the attachment.
    attachment_referenced: bool = False
    # Parsed attachment text, so the planner can carry it into notes without
    # re-deriving it from the merged prompt.
    attachment_excerpt: str = ""


class AttachmentContextEnricher:
    """附件上下文增强器"""

    def __init__(
        self,
        rag_service_url: str,
        rag_internal_token: str,
        attachment_store
    ):
        self.rag_service_url = rag_service_url.rstrip("/")
        self.rag_internal_token = rag_internal_token
        self.attachment_store = attachment_store

    def enrich(self, task_input: dict[str, Any]) -> EnrichedContext:
        """
        检测并增强任务输入

        输入: {"text": "根据这个附件创建日程", "attachment_ids": ["uuid"]}
        输出: EnrichedContext，enriched_text包含解析后的文档内容
        """
        text = str(task_input.get("text") or "")
        attachment_ids = task_input.get("attachment_ids") or []
        referenced = bool(attachment_ids) and references_attachment(text)

        if not attachment_ids:
            return EnrichedContext(
                original_text=text,
                enriched_text=text,
                attachment_metadata=[]
            )

        # 1. 解析所有附件
        parsed_contents = []
        metadata_list = []
        excerpt_parts = []

        for attachment_id in attachment_ids:
            metadata = self.attachment_store.get(attachment_id)
            if not metadata:
                logger.warning(f"附件不存在: {attachment_id}")
                continue

            try:
                parsed = self._call_rag_parse(attachment_id, metadata)
                blocks = parsed.get("blocks", [])
                if referenced:
                    # The user asked to use the document, so hand the planner
                    # the whole bounded body instead of a 10-block teaser.
                    content = self._build_full_content(blocks)
                else:
                    content = self._extract_relevant_content(parsed, text)
                parsed_contents.append({
                    "file_name": metadata["file_name"],
                    "content": content,
                    "page_count": parsed.get("page_count", 0)
                })
                metadata_list.append(metadata)
                if content and not content.startswith("[文档解析失败"):
                    excerpt_parts.append(f"【{metadata['file_name']}】\n{content}")
            except Exception as e:
                logger.error(f"附件解析失败 {attachment_id}: {str(e)}")
                parsed_contents.append({
                    "file_name": metadata["file_name"],
                    "content": f"[文档解析失败: {str(e)}]",
                    "page_count": 0
                })

        # 2. 构造增强上下文
        if not parsed_contents:
            enriched_text = text
        else:
            parts = [text, "\n\n--- 附件内容 ---\n"]
            for pc in parsed_contents:
                parts.append(f"\n【文档：{pc['file_name']}】")
                parts.append(pc["content"])
            enriched_text = "\n".join(parts)

        return EnrichedContext(
            original_text=text,
            enriched_text=enriched_text,
            attachment_metadata=metadata_list,
            attachment_referenced=referenced,
            attachment_excerpt="\n\n".join(excerpt_parts),
        )

    def _call_rag_parse(self, attachment_id: str, metadata: dict) -> dict:
        """调用RAG内部解析API"""
        with httpx.Client(timeout=420) as client:
            response = client.post(
                f"{self.rag_service_url}/internal/v1/parse-attachment",
                json={
                    "attachment_id": attachment_id,
                    "owner_id": metadata["owner_id"],
                    "file_name": metadata["file_name"],
                    "mime_type": metadata["mime_type"]
                },
                headers={
                    "x-rag-internal-token": self.rag_internal_token
                }
            )
            response.raise_for_status()
            return response.json()

    def _build_full_content(self, blocks: list[dict]) -> str:
        """Bounded full body, preserving page numbers and heading paths."""

        parts: list[str] = []
        total = 0
        for block in blocks:
            text = str(block.get("text") or "").strip()
            if not text and block.get("type") != "image":
                continue
            page = block.get("page_number") or "?"
            kind = str(block.get("type") or "text")
            heading_path = [str(item) for item in (block.get("heading_path") or []) if str(item).strip()]
            if kind in {"title", "heading"}:
                line = f"\n## [第{page}页] {text}"
            elif heading_path:
                line = f"[第{page}页][{' > '.join(heading_path)}] {text}"
            else:
                line = f"[第{page}页] {text}"
            total += len(line) + 1
            if total > 30000:
                parts.append("\n[内容过长已截断]")
                break
            parts.append(line)
        return "\n".join(parts)

    def _extract_relevant_content(self, parsed: dict, question: str) -> str:
        """提取与问题相关的内容（简化BM25）"""
        blocks = parsed.get("blocks", [])
        question_lower = question.lower()

        # 关键词提取
        keywords = [
            w for w in question_lower.split()
            if len(w) > 1 and w not in {"的", "了", "是", "在", "有", "这个", "那个", "根据"}
        ][:10]

        # 总结类问题
        if any(w in question_lower for w in ["总结", "概括", "讲了什么", "主要内容"]):
            return self._build_summary_content(blocks)

        # 关键词匹配
        scored = []
        for block in blocks:
            text = block["text"].lower()
            score = sum(text.count(kw) * 2 for kw in keywords)
            if block["type"] in ["title", "heading"]:
                score += 5
            if score > 0:
                scored.append((block, score))

        # 无匹配：返回文档开头
        if not scored:
            return self._build_fallback_content(blocks)

        # 排序并取前15个
        scored.sort(key=lambda x: x[1], reverse=True)
        top_blocks = [b for b, s in scored[:15]]

        # 按页码重排
        top_blocks.sort(key=lambda b: b["page_number"] or 0)

        # 拼接
        parts = []
        for block in top_blocks:
            page = block["page_number"] or "?"
            if block["type"] in ["title", "heading"]:
                parts.append(f"\n## [第{page}页] {block['text']}")
            else:
                parts.append(f"[第{page}页] {block['text']}")

        content = "\n".join(parts)
        if len(content) > 30000:
            content = content[:30000] + "\n\n[内容过长已截断]"

        return content

    def _build_summary_content(self, blocks: list[dict]) -> str:
        """构建总结类问题的内容"""
        parts = ["文档结构：\n"]

        # 提取标题
        titles = [b for b in blocks if b["type"] in ["title", "heading"]][:10]
        for t in titles:
            level = "  " * (int(t.get("level") or 1) - 1)
            parts.append(f"{level}- {t['text']}")

        # 前3段内容
        parts.append("\n主要内容：\n")
        text_blocks = [b for b in blocks if b["type"] == "text"][:3]
        for b in text_blocks:
            page = b["page_number"] or "?"
            parts.append(f"[第{page}页] {b['text'][:200]}...")

        return "\n".join(parts)

    def _build_fallback_content(self, blocks: list[dict]) -> str:
        """无匹配时的降级内容"""
        parts = ["以下是文档主要内容：\n"]
        for b in blocks[:10]:
            page = b["page_number"] or "?"
            parts.append(f"[第{page}页] {b['text'][:200]}...")
        return "\n".join(parts)
