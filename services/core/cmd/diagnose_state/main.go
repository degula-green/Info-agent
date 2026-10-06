package main

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

const (
	lycUserID = "7d0779ab-9ea4-409e-a51c-842b5b9fb875"
	orgID     = "61d4401e-5f94-4ade-a907-fab8e2c6db9b"
	itemID    = "ae6d2160-3c7f-427f-9098-111fd0fbcdf0"
)

func main() {
	values, err := loadEnv(".env")
	if err != nil {
		panic(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	db, err := pgx.Connect(ctx, values["CORE_DATABASE_URL"])
	if err != nil {
		panic(err)
	}
	defer db.Close(ctx)

	fmt.Println("== organization membership and roles ==")
	rows, err := db.Query(ctx, `
		SELECT m.user_id::text, u.nickname, m.status, COALESCE(string_agg(mr.role_code, ',' ORDER BY mr.role_code), '')
		FROM iam.organization_memberships m
		JOIN iam.users u ON u.id=m.user_id
		LEFT JOIN iam.membership_roles mr ON mr.membership_id=m.id AND mr.revoked_at IS NULL
		WHERE m.organization_id=$1::uuid AND (m.user_id=$2::uuid OR mr.role_code IN ('owner','information_admin'))
		GROUP BY m.user_id, u.nickname, m.status
		ORDER BY m.user_id`, orgID, lycUserID)
	if err != nil {
		panic(err)
	}
	for rows.Next() {
		var userID, nickname, status, roles string
		if err := rows.Scan(&userID, &nickname, &status, &roles); err != nil {
			panic(err)
		}
		fmt.Printf("user=%s nickname=%q status=%s roles=%s\n", userID, nickname, status, roles)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		panic(err)
	}

	fmt.Println("== knowledge item ==")
	var sourceMessage, scope, lifecycle, processing, sensitivity, visibility, originalRequired, permissionReady, aclStatus string
	var aclVersion int64
	err = db.QueryRow(ctx, `
		SELECT COALESCE(source_message_id::text,''), knowledge_scope, lifecycle_status, processing_status,
		       COALESCE(sensitivity,''), content_visibility, original_access_required::text,
		       permission_ready::text, acl_sync_status, acl_version
		FROM knowledge.knowledge_items WHERE id=$1::uuid`, itemID).
		Scan(&sourceMessage, &scope, &lifecycle, &processing, &sensitivity, &visibility, &originalRequired, &permissionReady, &aclStatus, &aclVersion)
	if err != nil {
		panic(err)
	}
	fmt.Printf("item=%s source_message=%s scope=%s lifecycle=%s processing=%s sensitivity=%s visibility=%s original_required=%s permission_ready=%s acl_status=%s acl_version=%d\n", itemID, sourceMessage, scope, lifecycle, processing, sensitivity, visibility, originalRequired, permissionReady, aclStatus, aclVersion)

	fmt.Println("== access requests ==")
	rows, err = db.Query(ctx, `
		SELECT id::text, requester_user_id::text, resource_type, resource_id::text, action, status,
		       fga_sync_status, COALESCE(last_error,''), created_at
		FROM iam.access_requests
		WHERE organization_id=$1::uuid OR requester_user_id=$2::uuid
		ORDER BY created_at DESC LIMIT 50`, orgID, lycUserID)
	if err != nil {
		panic(err)
	}
	for rows.Next() {
		var id, requester, resourceType, resourceID, action, status, syncStatus, lastError string
		var createdAt time.Time
		if err := rows.Scan(&id, &requester, &resourceType, &resourceID, &action, &status, &syncStatus, &lastError, &createdAt); err != nil {
			panic(err)
		}
		fmt.Printf("id=%s requester=%s resource=%s:%s action=%s status=%s fga=%s error=%q created=%s\n", id, requester, resourceType, resourceID, action, status, syncStatus, lastError, createdAt.Format(time.RFC3339))
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		panic(err)
	}

	fmt.Println("== deletion requests for known item/message ==")
	rows, err = db.Query(ctx, `
		SELECT r.id::text, r.requester_user_id::text, r.scope_id::text, r.status, r.idempotency_key,
		       r.requested_at, COALESCE(t.visibility_state,''), COALESCE(t.vector_state,''),
		       COALESCE(t.object_state,''), COALESCE(t.auth_state,''), COALESCE(m.lifecycle_status,'')
		FROM knowledge.deletion_requests r
		LEFT JOIN knowledge.deletion_targets t ON t.deletion_request_id=r.id
		LEFT JOIN knowledge.messages m ON m.id=t.resource_id
		WHERE r.scope_id=$1::uuid OR t.knowledge_item_id=$2::uuid OR t.resource_id=$1::uuid
		ORDER BY r.requested_at DESC`, sourceMessage, itemID)
	if err != nil {
		panic(err)
	}
	for rows.Next() {
		var id, requester, scopeID, status, key, visibility, vector, object, auth, lifecycle string
		var requestedAt time.Time
		if err := rows.Scan(&id, &requester, &scopeID, &status, &key, &requestedAt, &visibility, &vector, &object, &auth, &lifecycle); err != nil {
			panic(err)
		}
		fmt.Printf("id=%s requester=%s scope_id=%s status=%s key=%s requested=%s target=(visibility:%s vector:%s object:%s auth:%s message_lifecycle:%s)\n", id, requester, scopeID, status, key, requestedAt.Format(time.RFC3339), visibility, vector, object, auth, lifecycle)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		panic(err)
	}

	fmt.Println("== duplicate deletion groups ==")
	rows, err = db.Query(ctx, `
		SELECT r.scope_type, r.scope_id::text, count(*)::int,
		       string_agg(r.id::text || ':' || r.status, ',' ORDER BY r.requested_at DESC)
		FROM knowledge.deletion_requests r
		GROUP BY r.scope_type, r.scope_id
		HAVING count(*) > 1
		ORDER BY max(r.requested_at) DESC LIMIT 50`)
	if err != nil {
		panic(err)
	}
	for rows.Next() {
		var scopeType, scopeID, records string
		var count int
		if err := rows.Scan(&scopeType, &scopeID, &count, &records); err != nil {
			panic(err)
		}
		fmt.Printf("scope=%s:%s count=%d records=%s\n", scopeType, scopeID, count, records)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		panic(err)
	}
	fmt.Println("== duplicate deletion details ==")
	rows, err = db.Query(ctx, `
		SELECT r.scope_type, r.scope_id::text, r.id::text, r.requester_user_id::text, r.status,
		       t.resource_id::text, COALESCE(t.knowledge_item_id::text,''), COALESCE(m.lifecycle_status,''),
		       COALESCE(m.delete_request_id::text,''), t.visibility_state, t.vector_state, t.object_state, t.auth_state
		FROM knowledge.deletion_requests r
		JOIN knowledge.deletion_requests duplicate ON duplicate.scope_type=r.scope_type AND duplicate.scope_id=r.scope_id AND duplicate.id<>r.id
		LEFT JOIN knowledge.deletion_targets t ON t.deletion_request_id=r.id
		LEFT JOIN knowledge.messages m ON m.id=t.resource_id
		ORDER BY r.scope_id, r.requested_at DESC`)
	if err != nil {
		panic(err)
	}
	for rows.Next() {
		var scopeType, scopeID, id, requester, status, resourceID, itemID, lifecycle, deleteRequestID, visibility, vector, object, auth string
		if err := rows.Scan(&scopeType, &scopeID, &id, &requester, &status, &resourceID, &itemID, &lifecycle, &deleteRequestID, &visibility, &vector, &object, &auth); err != nil {
			panic(err)
		}
		fmt.Printf("scope=%s:%s id=%s requester=%s status=%s resource=%s item=%s lifecycle=%s message_delete_request=%s target=(%s,%s,%s,%s)\n", scopeType, scopeID, id, requester, status, resourceID, itemID, lifecycle, deleteRequestID, visibility, vector, object, auth)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		panic(err)
	}
	}

func loadEnv(path string) (map[string]string, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	values := map[string]string{}
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		values[strings.TrimSpace(key)] = strings.Trim(strings.TrimSpace(value), "\"")
	}
	return values, scanner.Err()
}
