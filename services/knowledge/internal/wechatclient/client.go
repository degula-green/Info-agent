package wechatclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type Client struct {
	BaseURL, Token string
	HTTP           *http.Client
}

func New(baseURL, token string) *Client {
	return &Client{BaseURL: strings.TrimRight(baseURL, "/"), Token: token, HTTP: &http.Client{Timeout: 30 * time.Second}}
}

func (c *Client) do(ctx context.Context, method, path string, in, out any) error {
	var body *bytes.Reader
	if in == nil {
		body = bytes.NewReader(nil)
	} else {
		raw, err := json.Marshal(in)
		if err != nil {
			return err
		}
		body = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, body)
	if err != nil {
		return err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Content-Type", "application/json")
	if c.Token != "" {
		req.Header.Set("X-Collector-Token", c.Token)
	}
	res, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode >= 400 {
		var e struct {
			Detail  string `json:"detail"`
			Message string `json:"message"`
		}
		_ = json.NewDecoder(res.Body).Decode(&e)
		msg := e.Detail
		if msg == "" {
			msg = e.Message
		}
		if msg == "" {
			msg = res.Status
		}
		return fmt.Errorf("wechat collector: %s", msg)
	}
	if out != nil {
		return json.NewDecoder(res.Body).Decode(out)
	}
	return nil
}

func (c *Client) Bind(ctx context.Context, wxid, dbDir string, rebind bool) (map[string]any, error) {
	path := "/bind"
	if rebind {
		path = "/rebind"
	}
	var out map[string]any
	err := c.do(ctx, http.MethodPost, path, map[string]string{"wxid": wxid, "db_dir": dbDir}, &out)
	return out, err
}
func (c *Client) Status(ctx context.Context) (map[string]any, error) {
	var out map[string]any
	err := c.do(ctx, http.MethodGet, "/status", nil, &out)
	return out, err
}
func (c *Client) Stop(ctx context.Context) error {
	return c.do(ctx, http.MethodPost, "/stop", nil, nil)
}
func (c *Client) Conversations(ctx context.Context) (map[string]any, error) {
	var out map[string]any
	err := c.do(ctx, http.MethodGet, "/conversations", nil, &out)
	return out, err
}
func (c *Client) Contacts(ctx context.Context, keyword string) (map[string]any, error) {
	var out map[string]any
	path := "/contacts"
	if strings.TrimSpace(keyword) != "" {
		path += "?keyword=" + url.QueryEscape(keyword)
	}
	err := c.do(ctx, http.MethodGet, path, nil, &out)
	return out, err
}
func (c *Client) Config(ctx context.Context) (map[string]any, error) {
	var out map[string]any
	err := c.do(ctx, http.MethodGet, "/config", nil, &out)
	return out, err
}
func (c *Client) SaveConfig(ctx context.Context, value any) (map[string]any, error) {
	var out map[string]any
	err := c.do(ctx, http.MethodPut, "/config", value, &out)
	return out, err
}

func (c *Client) Bootstrap(ctx context.Context, connectorID string) (map[string]any, error) {
	var out map[string]any
	err := c.do(ctx, http.MethodGet, "/internal/wechat/bootstrap?connector_id="+url.QueryEscape(connectorID), nil, &out)
	return out, err
}
