// Package cexpay 是 multi-cex-pay 的商户端 SDK（只用标准库）。
//
//	client := cexpay.New("http://127.0.0.1:8787", cexpay.Options{WebhookSecret: "..."})
//	res, err := client.CreateOrder(cexpay.CreateOrderRequest{Amount: "9.9", MerchantRef: "SHOP-1"})
//
// 回调验签（务必用原始 body）：
//
//	body, _ := io.ReadAll(r.Body)
//	ok := client.VerifyWebhook(body, r.Header.Get("X-CexPay-Timestamp"),
//	                           r.Header.Get("X-CexPay-Signature"))
package cexpay

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Options 客户端可选参数。
type Options struct {
	WebhookSecret string
	AdminToken    string
	Timeout       time.Duration
}

// Client 商户端。
type Client struct {
	BaseURL string
	opts    Options
	http    *http.Client
}

// New 创建客户端。
func New(baseURL string, opts Options) *Client {
	if opts.Timeout == 0 {
		opts.Timeout = 10 * time.Second
	}
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		opts:    opts,
		http:    &http.Client{Timeout: opts.Timeout},
	}
}

// Order 订单。
type Order struct {
	OrderID     string                 `json:"order_id"`
	MerchantRef string                 `json:"merchant_ref"`
	Exchange    string                 `json:"exchange"`
	BaseAmount  string                 `json:"base_amount"`
	PayAmount   string                 `json:"pay_amount"`
	Currency    string                 `json:"currency"`
	Status      string                 `json:"status"`
	Memo        string                 `json:"memo"`
	CreatedMS   int64                  `json:"created_ms"`
	ExpiresMS   int64                  `json:"expires_ms"`
	ExpiresInS  int64                  `json:"expires_in_s"`
	PaidMS      *int64                 `json:"paid_ms"`
	Metadata    map[string]any         `json:"metadata"`
	Settlement  *Settlement            `json:"settlement,omitempty"`
}

// Settlement 核销信息。
type Settlement struct {
	Exchange string `json:"exchange"`
	TxID     string `json:"tx_id"`
	Tier     int    `json:"tier"`
	Reason   string `json:"reason"`
}

// IsPaid 是否已支付。
func (o *Order) IsPaid() bool { return o.Status == "paid" }

// CreateOrderRequest 下单参数。
type CreateOrderRequest struct {
	Amount      string         `json:"amount"`
	Exchange    string         `json:"exchange,omitempty"`
	MerchantRef string         `json:"merchant_ref,omitempty"`
	CallbackURL string         `json:"callback_url,omitempty"`
	TTLSeconds  int            `json:"ttl_s,omitempty"`
	Metadata    map[string]any `json:"metadata,omitempty"`
}

// CreateOrderResponse 下单返回。
type CreateOrderResponse struct {
	Order       Order  `json:"order"`
	CheckoutURL string `json:"checkout_url"`
	QRURL       string `json:"qr_url"`
}

// CheckResponse 主动核销返回。
type CheckResponse struct {
	Order   Order    `json:"order"`
	IsPaid  bool     `json:"is_paid"`
	Scanned int      `json:"scanned"`
	Errors  []string `json:"errors"`
}

// APIError 服务端返回的错误。
type APIError struct {
	Status int
	Detail string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("cexpay: HTTP %d: %s", e.Status, e.Detail)
}

func (c *Client) call(method, path string, body any, admin bool, out any) error {
	var reader io.Reader
	if body != nil {
		blob, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(blob)
	}

	req, err := http.NewRequest(method, c.BaseURL+path, reader)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if admin {
		if c.opts.AdminToken == "" {
			return fmt.Errorf("cexpay: 该接口需要 AdminToken")
		}
		req.Header.Set("Authorization", "Bearer "+c.opts.AdminToken)
	}

	res, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("cexpay: 无法连接 %s%s: %w", c.BaseURL, path, err)
	}
	defer res.Body.Close()

	raw, err := io.ReadAll(res.Body)
	if err != nil {
		return err
	}
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		var wrapper struct {
			Detail string `json:"detail"`
		}
		_ = json.Unmarshal(raw, &wrapper)
		detail := wrapper.Detail
		if detail == "" {
			detail = string(raw)
		}
		return &APIError{Status: res.StatusCode, Detail: detail}
	}
	if out == nil {
		return nil
	}
	return json.Unmarshal(raw, out)
}

// CreateOrder 创建订单。相同 MerchantRef 会复用未过期的待付订单（幂等）。
func (c *Client) CreateOrder(req CreateOrderRequest) (*CreateOrderResponse, error) {
	var out CreateOrderResponse
	if err := c.call(http.MethodPost, "/api/orders", req, false, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// GetOrder 查询订单。
func (c *Client) GetOrder(orderID string) (*Order, error) {
	var out struct {
		Order Order `json:"order"`
	}
	if err := c.call(http.MethodGet, "/api/orders/"+orderID, nil, false, &out); err != nil {
		return nil, err
	}
	return &out.Order, nil
}

// CheckOrder 主动催一次核销（用户点「我已支付」时用）。
func (c *Client) CheckOrder(orderID string) (*CheckResponse, error) {
	var out CheckResponse
	if err := c.call(http.MethodPost, "/api/orders/"+orderID+"/check", nil, false, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// CancelOrder 取消订单。
func (c *Client) CancelOrder(orderID string) (*Order, error) {
	var out struct {
		Order Order `json:"order"`
	}
	if err := c.call(http.MethodPost, "/api/orders/"+orderID+"/cancel", nil, false, &out); err != nil {
		return nil, err
	}
	return &out.Order, nil
}

// WaitForPayment 阻塞等待支付结果。生产环境建议改用 webhook。
func (c *Client) WaitForPayment(orderID string, timeout, interval time.Duration) (*Order, error) {
	if interval == 0 {
		interval = 5 * time.Second
	}
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		order, err := c.GetOrder(orderID)
		if err != nil {
			return nil, err
		}
		if order.Status != "pending" {
			return order, nil
		}
		time.Sleep(interval)
	}
	return nil, fmt.Errorf("cexpay: 等待超时，订单 %s 仍未支付", orderID)
}

// VerifyWebhook 校验回调。rawBody 必须是原始字节，别先反序列化再重新编码。
func (c *Client) VerifyWebhook(rawBody []byte, timestamp, signature string) bool {
	return c.VerifyWebhookWithTolerance(rawBody, timestamp, signature, 300*time.Second)
}

// VerifyWebhookWithTolerance 同上，可自定义时间容差（0 表示不校验时间）。
func (c *Client) VerifyWebhookWithTolerance(
	rawBody []byte, timestamp, signature string, tolerance time.Duration,
) bool {
	if c.opts.WebhookSecret == "" {
		return false
	}
	stamp, err := strconv.ParseInt(strings.TrimSpace(timestamp), 10, 64)
	if err != nil {
		return false
	}
	if tolerance > 0 {
		drift := math.Abs(float64(time.Now().Unix() - stamp))
		if drift > tolerance.Seconds() {
			return false // 拒绝重放
		}
	}

	mac := hmac.New(sha256.New, []byte(c.opts.WebhookSecret))
	mac.Write([]byte(strconv.FormatInt(stamp, 10) + "." + string(rawBody)))
	expected := hex.EncodeToString(mac.Sum(nil))
	return hmac.Equal([]byte(expected), []byte(strings.TrimSpace(signature)))
}

// WebhookEvent 回调事件体。
type WebhookEvent struct {
	Event string `json:"event"`
	Order Order  `json:"order"`
}

// ParseWebhook 先验签再反序列化。
func (c *Client) ParseWebhook(rawBody []byte, timestamp, signature string) (*WebhookEvent, error) {
	if !c.VerifyWebhook(rawBody, timestamp, signature) {
		return nil, fmt.Errorf("cexpay: 回调签名校验失败")
	}
	var event WebhookEvent
	if err := json.Unmarshal(rawBody, &event); err != nil {
		return nil, err
	}
	return &event, nil
}
