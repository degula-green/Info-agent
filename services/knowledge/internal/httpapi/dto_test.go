package httpapi

import (
	"testing"

	"info-agent/knowledge/internal/domain"
)

func TestPublicAttachmentNormalizesLegacyWechatImageMetadata(t *testing.T) {
	value := domain.Attachment{
		ID:                "att-1",
		FileName:          "image.bin",
		MIMEType:          "application/octet-stream",
		PreviewCapability: "download",
		ContentStatus:     "ready",
	}
	out := publicAttachmentFromDomain(value)
	if out.FileName != "image.jpg" || out.MIMEType != "image/jpeg" || out.PreviewCapability != "preview" {
		t.Fatalf("legacy image metadata was not normalized: %+v", out)
	}
}

func TestPublicAttachmentKeepsUnknownBinaryMetadata(t *testing.T) {
	value := domain.Attachment{FileName: "attachment.bin", MIMEType: "application/octet-stream", PreviewCapability: "download"}
	out := publicAttachmentFromDomain(value)
	if out.FileName != value.FileName || out.MIMEType != value.MIMEType || out.PreviewCapability != value.PreviewCapability {
		t.Fatalf("unknown binary metadata was unexpectedly changed: %+v", out)
	}
}

func TestNormalizedAttachmentMetadataInfersMimeFromExtension(t *testing.T) {
	name, mimeType := normalizedAttachmentMetadata("report.pdf", "")
	if name != "report.pdf" || mimeType != "application/pdf" {
		t.Fatalf("extension MIME inference failed: name=%q mime=%q", name, mimeType)
	}
}
