"""Vision descriptions: the client, the enricher and their degradation."""

import json

import pytest

from app.infrastructure.vision.client import VisionError


class FakeVision:
    model = "fake-vision"

    def __init__(self, text="夜晚的乡间道路，路边有一条水沟。", fail=False):
        self.text = text
        self.fail = fail
        self.calls = []

    def describe(self, data, mime_type):
        self.calls.append((data, mime_type))
        if self.fail:
            raise VisionError("boom")
        return self.text


class FakeCache:
    def __init__(self):
        self.data = {}
        self.writes = []

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.writes.append((key, ttl, value))
        self.data[key] = value


def _document(*blocks):
    from app.domain.models import ParsedDocument

    return ParsedDocument(
        markdown="",
        blocks=list(blocks),
        parser="mineru-vlm",
        parser_version="test",
    )


def _image_block(**kwargs):
    from app.domain.models import CanonicalBlock

    values = {"page_number": 1, "order": 0, "type": "image", "text": ""}
    values.update(kwargs)
    return CanonicalBlock(**values)


def test_vision_client_sends_the_picture_and_reads_the_answer():
    from app.infrastructure.http import HttpResult
    from app.infrastructure.vision.client import VisionClient

    class FakeHttp:
        def __init__(self):
            self.payload = None

        def request(self, method, url, **kwargs):
            self.payload = kwargs.get("body")
            return HttpResult(
                200,
                {},
                json.dumps(
                    {"choices": [{"message": {"content": "夜晚的乡间道路"}}]}
                ).encode("utf-8"),
            )

    http = FakeHttp()
    client = VisionClient(
        base_url="https://example.com/v1", api_key="k", http=http
    )

    text = client.describe(b"\x89PNG-bytes", "image/png")

    assert text == "夜晚的乡间道路"
    parts = http.payload["messages"][0]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_client_requires_a_base_url():
    from app.infrastructure.vision.client import VisionClient

    with pytest.raises(VisionError):
        VisionClient(base_url="").describe(b"x", "image/png")


def test_vision_client_rejects_an_answer_without_choices():
    from app.infrastructure.http import HttpResult
    from app.infrastructure.vision.client import VisionClient

    class FakeHttp:
        def request(self, method, url, **kwargs):
            return HttpResult(200, {}, b"{}")

    client = VisionClient(base_url="https://example.com/v1", http=FakeHttp())

    with pytest.raises(VisionError):
        client.describe(b"x", "image/png")


def test_enricher_describes_an_uploaded_picture(tmp_path):
    from app.application.processing.image_enrichment import (
        ImageDescriptionEnricher,
    )

    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpeg-bytes")
    vision = FakeVision()
    cache = FakeCache()
    enricher = ImageDescriptionEnricher(vision, cache=cache)

    result = enricher.enrich(
        _document(_image_block()),
        root=tmp_path / "absent",
        source_path=image,
        source_mime_type="image/jpeg",
    )

    assert result.blocks[0].text == vision.text
    assert result.blocks[0].metadata["description_source"] == "vision"
    assert len(cache.writes) == 1
    assert vision.calls == [(b"jpeg-bytes", "image/jpeg")]


def test_enricher_reads_the_picture_inside_the_parse_output(tmp_path):
    from app.application.processing.image_enrichment import (
        ImageDescriptionEnricher,
    )

    root = tmp_path / "result"
    (root / "images").mkdir(parents=True)
    (root / "images" / "figure.png").write_bytes(b"png-bytes")
    vision = FakeVision()
    enricher = ImageDescriptionEnricher(vision)

    result = enricher.enrich(
        _document(_image_block(asset_ref="images/figure.png")), root=root
    )

    assert result.blocks[0].text == vision.text
    assert vision.calls == [(b"png-bytes", "image/png")]


def test_enricher_reuses_a_cached_description(tmp_path):
    from app.application.processing.image_enrichment import (
        ImageDescriptionEnricher,
    )

    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpeg-bytes")
    cache = FakeCache()
    first = FakeVision()
    ImageDescriptionEnricher(first, cache=cache).enrich(
        _document(_image_block()),
        root=tmp_path,
        source_path=image,
        source_mime_type="image/jpeg",
    )

    second = FakeVision(text="不该被调用")
    result = ImageDescriptionEnricher(second, cache=cache).enrich(
        _document(_image_block()),
        root=tmp_path,
        source_path=image,
        source_mime_type="image/jpeg",
    )

    assert result.blocks[0].text == first.text
    assert second.calls == []


def test_oversized_uploaded_picture_is_refused(tmp_path):
    from app.application.processing.image_enrichment import (
        ImageDescriptionEnricher,
        ImageTooLargeError,
    )

    image = tmp_path / "big.jpg"
    image.write_bytes(b"x" * 100)
    enricher = ImageDescriptionEnricher(FakeVision(), max_image_bytes=10)

    with pytest.raises(ImageTooLargeError):
        enricher.enrich(
            _document(_image_block()),
            root=tmp_path,
            source_path=image,
            source_mime_type="image/jpeg",
        )


def test_oversized_inline_picture_is_skipped_not_refused(tmp_path):
    from app.application.processing.image_enrichment import (
        ImageDescriptionEnricher,
    )

    root = tmp_path / "result"
    (root / "images").mkdir(parents=True)
    (root / "images" / "big.png").write_bytes(b"x" * 100)
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF")
    vision = FakeVision()
    enricher = ImageDescriptionEnricher(vision, max_image_bytes=10)

    result = enricher.enrich(
        _document(_image_block(asset_ref="images/big.png")),
        root=root,
        source_path=report,
        source_mime_type="application/pdf",
    )

    # The document itself is still usable: only the oversized picture is left
    # undescribed.
    assert result.blocks[0].text == ""
    assert vision.calls == []


def test_vision_failure_leaves_the_block_unchanged(tmp_path):
    from app.application.processing.image_enrichment import (
        ImageDescriptionEnricher,
    )

    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpeg-bytes")
    enricher = ImageDescriptionEnricher(FakeVision(fail=True))

    result = enricher.enrich(
        _document(_image_block()),
        root=tmp_path,
        source_path=image,
        source_mime_type="image/jpeg",
    )

    assert result.blocks[0].text == ""


def test_picture_with_existing_text_is_left_alone(tmp_path):
    from app.application.processing.image_enrichment import (
        ImageDescriptionEnricher,
    )

    image = tmp_path / "photo.jpg"
    image.write_bytes(b"jpeg-bytes")
    vision = FakeVision()
    enricher = ImageDescriptionEnricher(vision)

    result = enricher.enrich(
        _document(_image_block(text="MinerU 已经给出的说明")),
        root=tmp_path,
        source_path=image,
        source_mime_type="image/jpeg",
    )

    assert result.blocks[0].text == "MinerU 已经给出的说明"
    assert vision.calls == []


def test_enricher_caps_the_number_of_pictures(tmp_path):
    from app.application.processing.image_enrichment import (
        ImageDescriptionEnricher,
    )

    root = tmp_path / "result"
    (root / "images").mkdir(parents=True)
    blocks = []
    for index in range(3):
        (root / "images" / f"{index}.png").write_bytes(f"png-{index}".encode())
        blocks.append(_image_block(order=index, asset_ref=f"images/{index}.png"))
    vision = FakeVision()
    enricher = ImageDescriptionEnricher(vision, max_images=2)

    result = enricher.enrich(_document(*blocks), root=root)

    assert len(vision.calls) == 2
    assert [bool(block.text) for block in result.blocks] == [True, True, False]
