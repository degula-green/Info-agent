package main

import (
  "context"
  "fmt"
  "os"
  "github.com/jackc/pgx/v5"
)

func main() {
  conn, err := pgx.Connect(context.Background(), os.Getenv("KNOWLEDGE_DATABASE_URL")); if err != nil { panic(err) }; defer conn.Close(context.Background())
  rows, err := conn.Query(context.Background(), `SELECT external_attachment_id,file_name,mime_type,size_bytes,COALESCE(content_hash,''),content_status,COALESCE(object_ref,'') FROM knowledge.attachments WHERE external_attachment_id LIKE '%:18335:%'`); if err != nil { panic(err) }; defer rows.Close()
  for rows.Next() { var id, name, mime, hash, status, object string; var size int64; if err := rows.Scan(&id,&name,&mime,&size,&hash,&status,&object); err != nil { panic(err) }; fmt.Printf("%s name=%s mime=%s size=%d hash=%s status=%s object=%s\n",id,name,mime,size,hash,status,object) }
}
