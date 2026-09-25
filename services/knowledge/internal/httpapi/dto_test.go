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

func TestPublicContactDetailUsesProfileFactsAndAttachments(t *testing.T) {
	value := domain.ContactDetail{
		ContactView: domain.ContactView{ID: "contact-1", Kind: "external", DisplayName: "联系人"},
		Profile:     domain.ContactProfile{ContactKey: "contact-1", Status: "ready", Summary: "简介"},
		Facts: []domain.ContactFact{{
			ID: "fact-1", FactType: "phone", Label: "手机号",
			Access: domain.ContactAccess{Status: "locked"},
		}},
		Attachments: []domain.Attachment{{
			ID: "attachment-1", ConversationID: "conversation-1", ExternalAttachmentID: "external-1",
			FileName: "report.pdf", MIMEType: "application/pdf", ContentVersion: 1, ContentStatus: "ready",
			Access: domain.ContactAccess{Status: "granted"},
		}},
	}
	out := publicContactDetailFromDomain(value)
	if out.Contact.ID != "contact-1" || out.Profile.Summary != "简介" || len(out.Facts) != 1 || len(out.Attachments) != 1 {
		t.Fatalf("unexpected contact detail projection: %+v", out)
	}
	if out.Facts[0].Access.Status != "locked" || out.Attachments[0].Access.Status != "granted" {
		t.Fatalf("contact access state was not projected: %+v", out)
	}
}
