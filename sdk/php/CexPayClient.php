<?php
/**
 * multi-cex-pay 商户端 SDK（PHP ≥ 7.4，只用 curl 扩展）
 *
 *   require 'CexPayClient.php';
 *   $client = new CexPayClient('http://127.0.0.1:8787', ['webhook_secret' => '...']);
 *   $res = $client->createOrder('9.9', ['merchant_ref' => 'SHOP-1001']);
 *   header('Location: ' . $client->baseUrl . $res['checkout_url']);
 *
 * 回调验签（务必用原始 body）：
 *   $raw = file_get_contents('php://input');
 *   $ok = $client->verifyWebhook($raw, $_SERVER['HTTP_X_CEXPAY_TIMESTAMP'],
 *                                $_SERVER['HTTP_X_CEXPAY_SIGNATURE']);
 */

class CexPayException extends RuntimeException {}

class CexPayClient
{
    public string $baseUrl;
    private ?string $webhookSecret;
    private ?string $adminToken;
    private int $timeout;

    public function __construct(string $baseUrl, array $options = [])
    {
        $this->baseUrl = rtrim($baseUrl, '/');
        $this->webhookSecret = $options['webhook_secret'] ?? null;
        $this->adminToken = $options['admin_token'] ?? null;
        $this->timeout = $options['timeout'] ?? 10;
    }

    private function call(string $method, string $path, ?array $body = null, bool $admin = false): array
    {
        $headers = ['Content-Type: application/json'];
        if ($admin) {
            if (!$this->adminToken) {
                throw new CexPayException('该接口需要 admin_token');
            }
            $headers[] = 'Authorization: Bearer ' . $this->adminToken;
        }

        $ch = curl_init($this->baseUrl . $path);
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CUSTOMREQUEST  => $method,
            CURLOPT_HTTPHEADER     => $headers,
            CURLOPT_TIMEOUT        => $this->timeout,
        ]);
        if ($body !== null) {
            curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body, JSON_UNESCAPED_UNICODE));
        }

        $raw = curl_exec($ch);
        if ($raw === false) {
            $error = curl_error($ch);
            curl_close($ch);
            throw new CexPayException("无法连接 {$this->baseUrl}{$path}: {$error}");
        }
        $status = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
        curl_close($ch);

        $data = json_decode($raw, true);
        if (!is_array($data)) {
            $data = ['detail' => $raw];
        }
        if ($status < 200 || $status >= 300) {
            throw new CexPayException($data['detail'] ?? "HTTP {$status}", $status);
        }
        return $data;
    }

    /** 创建订单。相同 merchant_ref 会复用未过期的待付订单（幂等）。 */
    public function createOrder($amount, array $options = []): array
    {
        $payload = ['amount' => (string) $amount];
        foreach (['exchange', 'merchant_ref', 'callback_url', 'ttl_s', 'metadata'] as $key) {
            if (isset($options[$key])) {
                $payload[$key] = $options[$key];
            }
        }
        return $this->call('POST', '/api/orders', $payload);
    }

    public function getOrder(string $orderId): array
    {
        return $this->call('GET', "/api/orders/{$orderId}")['order'];
    }

    /** 主动催一次核销（用户点「我已支付」时用）。 */
    public function checkOrder(string $orderId): array
    {
        return $this->call('POST', "/api/orders/{$orderId}/check");
    }

    public function cancelOrder(string $orderId): array
    {
        return $this->call('POST', "/api/orders/{$orderId}/cancel")['order'];
    }

    public function exchanges(): array
    {
        return $this->call('GET', '/api/exchanges')['exchanges'];
    }

    /** 校验回调。$rawBody 必须是 php://input 的原始内容。 */
    public function verifyWebhook(string $rawBody, $timestamp, ?string $signature, int $toleranceS = 300): bool
    {
        if (!$this->webhookSecret) {
            throw new CexPayException('未配置 webhook_secret');
        }
        if (!is_numeric($timestamp)) {
            return false;
        }
        $stamp = (int) $timestamp;
        if ($toleranceS > 0 && abs(time() - $stamp) > $toleranceS) {
            return false;   // 拒绝重放
        }
        $expected = hash_hmac('sha256', $stamp . '.' . $rawBody, $this->webhookSecret);
        return hash_equals($expected, (string) $signature);
    }

    public function adminSweep(): array
    {
        return $this->call('POST', '/api/admin/sweep', null, true);
    }
}
