package application

import (
	"context"
	"testing"
)

type recordingRelationWriter struct{ tuples []RelationTuple }

func (w *recordingRelationWriter) WriteRelations(_ context.Context, tuples []RelationTuple) error {
	w.tuples = append([]RelationTuple(nil), tuples...)
	return nil
}

type memoryACLVersions struct {
	fingerprint string
	version     int64
	members     []string
}

func (v *memoryACLVersions) ResolveACLVersion(_ context.Context, _ string, fingerprint string) (int64, error) {
	if v.version == 0 {
		v.version = 1
		v.fingerprint = fingerprint
	} else if v.fingerprint != fingerprint {
		v.version++
		v.fingerprint = fingerprint
	}
	return v.version, nil
}

func (v *memoryACLVersions) ListActiveOrganizationMemberIDs(context.Context, string) ([]string, error) {
	return append([]string(nil), v.members...), nil
}

func TestPermissionSyncBuildsOrganizationAttachmentRelationsAndStableVersion(t *testing.T) {
	writer := &recordingRelationWriter{}
	versions := &memoryACLVersions{members: []string{"member-1"}}
	service := NewPermissionSyncService(writer, versions)
	input := ResourcePermission{
		KnowledgeItemID: "ki-1", AttachmentID: "att-1", KnowledgeScope: "organization",
		OrganizationID: "org-1", ConversationID: "conversation-1",
		ParticipantUserIDs: []string{"participant-1"}, ContentAccessRequired: false,
	}
	first, err := service.Sync(context.Background(), input)
	if err != nil {
		t.Fatal(err)
	}
	if first.ACLVersion != 1 || first.RelationCount != 8 {
		t.Fatalf("unexpected first sync: %+v tuples=%+v", first, writer.tuples)
	}
	want := map[RelationTuple]bool{
		{User: "user:member-1", Relation: "member", Object: "organization:org-1"}:                                    true,
		{User: "user:participant-1", Relation: "participant", Object: "conversation_group:conversation-1"}:           true,
		{User: "conversation_group:conversation-1", Relation: "conversation_group", Object: "knowledge_item:ki-1"}:   true,
		{User: "conversation_group:conversation-1#member", Relation: "accessor", Object: "attachment_content:att-1"}: true,
	}
	for _, tuple := range writer.tuples {
		delete(want, tuple)
	}
	if len(want) != 0 {
		t.Fatalf("required permission tuples missing: %+v", want)
	}
	second, err := service.Sync(context.Background(), input)
	if err != nil || second.ACLVersion != first.ACLVersion {
		t.Fatalf("idempotent sync changed ACL version: first=%+v second=%+v err=%v", first, second, err)
	}
	input.ContentAccessRequired = true
	third, err := service.Sync(context.Background(), input)
	if err != nil || third.ACLVersion != 2 {
		t.Fatalf("changed permissions did not advance ACL version: %+v err=%v", third, err)
	}
	for _, tuple := range writer.tuples {
		if tuple.Object == "attachment_content:att-1" && tuple.Relation == "accessor" {
			t.Fatalf("protected attachment retained automatic content access: %+v", tuple)
		}
	}
}

func TestPermissionSyncRequiresOwnershipContext(t *testing.T) {
	service := NewPermissionSyncService(&recordingRelationWriter{}, &memoryACLVersions{})
	if _, err := service.Sync(context.Background(), ResourcePermission{KnowledgeItemID: "ki", KnowledgeScope: "private"}); err == nil {
		t.Fatal("private permission sync accepted without owner")
	}
}
