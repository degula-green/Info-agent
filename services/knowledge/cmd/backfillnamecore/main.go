// Command backfillnamecore recomputes contact_relations.name_core from the
// current remark, falling back to the stored display name. Running it after a
// rule change repairs rows that contain a stale or incorrectly split core.
//
// The collector-side remark sync remains the way to refresh the note itself.
package main

import (
	"context"
	"fmt"
	"log"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"info-agent/knowledge/internal/config"
	"info-agent/knowledge/internal/contactname"
)

type pendingRelation struct {
	relationID string
	sourceName string
}

func main() {
	cfg := config.Load()
	if strings.TrimSpace(cfg.DatabaseURL) == "" {
		log.Fatal("KNOWLEDGE_DATABASE_URL is required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()

	poolConfig, err := pgxpool.ParseConfig(cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("parse database url: %v", err)
	}
	poolConfig.ConnConfig.ConnectTimeout = 3 * time.Second
	pool, err := pgxpool.NewWithConfig(ctx, poolConfig)
	if err != nil {
		log.Fatalf("connect: %v", err)
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		log.Fatalf("ping: %v", err)
	}

	rows, err := pool.Query(ctx, `SELECT cr.id::text,
			COALESCE(NULLIF(cr.remark,''), ei.display_name, '')
		FROM knowledge.contact_relations cr
		JOIN knowledge.external_identities ei ON ei.id=cr.external_identity_id`)
	if err != nil {
		log.Fatalf("query pending relations: %v", err)
	}
	pending := []pendingRelation{}
	for rows.Next() {
		var item pendingRelation
		if err := rows.Scan(&item.relationID, &item.sourceName); err != nil {
			rows.Close()
			log.Fatalf("scan pending relation: %v", err)
		}
		pending = append(pending, item)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		log.Fatalf("iterate pending relations: %v", err)
	}
	rows.Close()

	updated, skipped := 0, 0
	for _, item := range pending {
		core := contactname.Extract(item.sourceName)
		if core == "" {
			skipped++
			continue
		}
		tag, err := pool.Exec(ctx, `UPDATE knowledge.contact_relations
			SET name_core=$2, updated_at=now()
			WHERE id=$1 AND COALESCE(name_core,'') IS DISTINCT FROM $2`, item.relationID, core)
		if err != nil {
			log.Fatalf("update relation %s: %v", item.relationID, err)
		}
		if tag.RowsAffected() > 0 {
			updated++
		} else {
			skipped++
		}
	}
	fmt.Printf("name-core backfill done: scanned=%d updated=%d skipped=%d\n", len(pending), updated, skipped)
}
