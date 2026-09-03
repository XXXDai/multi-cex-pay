<?php
/**
 * 最小可用的 PHP 回调端点。
 *
 * 本地跑一下：
 *   CEXPAY_WEBHOOK_SECRET=shhh php -S 127.0.0.1:8080 examples/webhook_receiver.php
 * 然后建单时把 callback_url 指到 http://127.0.0.1:8080/
 *
 * 只处理 order.paid 一个事件。验签逻辑全在 SDK 里，这里只负责：
 *   1. 拿原始 body
 *   2. 验签
 *   3. 幂等地落库 / 发货
 */

declare(strict_types=1);

require __DIR__ . '/../sdk/php/CexPayClient.php';

const LOG_PATH = __DIR__ . '/webhook.log';

$secret = getenv('CEXPAY_WEBHOOK_SECRET') ?: '';
$client = new CexPayClient(
    getenv('CEXPAY_GATEWAY_URL') ?: 'http://127.0.0.1:8787',
    ['webhook_secret' => $secret]
);

function respond(int $status, array $body): void
{
    http_response_code($status);
    header('Content-Type: application/json');
    echo json_encode($body, JSON_UNESCAPED_UNICODE);
    exit;
}

if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST') {
    respond(405, ['error' => 'only POST']);
}
if ($secret === '') {
    respond(500, ['error' => '没有设置 CEXPAY_WEBHOOK_SECRET']);
}

// 关键点一：必须用 php://input 的原始字节。
// 不要用 $_POST（那只解析表单编码），也不要 json_decode 后再 json_encode
// 回去：键序、空格、Unicode 转义都会变，HMAC 必然对不上。
$raw = file_get_contents('php://input');
if ($raw === false || $raw === '') {
    respond(400, ['error' => 'empty body']);
}

$timestamp = $_SERVER['HTTP_X_CEXPAY_TIMESTAMP'] ?? '';
$signature = $_SERVER['HTTP_X_CEXPAY_SIGNATURE'] ?? null;

// 签名是 hex(HMAC-SHA256("{timestamp}.{raw_body}", secret))，
// 同时校验时间戳在 300s 容忍窗内（防重放）。
if (!$client->verifyWebhook($raw, $timestamp, $signature)) {
    respond(400, ['error' => 'bad signature']);
}

$payload = json_decode($raw, true);
if (!is_array($payload)) {
    respond(400, ['error' => 'bad json']);
}
if (($payload['event'] ?? '') !== 'order.paid') {
    respond(200, ['ok' => true, 'ignored' => $payload['event'] ?? null]);
}

$order = $payload['order'] ?? [];
$orderId = $order['order_id'] ?? '';
if ($orderId === '') {
    respond(400, ['error' => 'missing order_id']);
}

// 关键点二：必须对 order_id 幂等。
// 网关的重试阶梯是 0/15s/1m/5m/30m/2h/6h，只要没在 2xx 之前处理完，
// 同一笔订单就可能被投递多次。
//
// 这里用一个 append-only 日志文件充当「已处理」记录，只为了让示例能独立跑起来。
// 生产上换成数据库：order_id 建唯一索引，插入冲突就当已处理，直接返回 200。
$seen = is_file(LOG_PATH) ? file_get_contents(LOG_PATH) : '';
if ($seen !== false && strpos($seen, "\t{$orderId}\t") !== false) {
    respond(200, ['ok' => true, 'note' => 'already processed']);
}

$line = sprintf(
    "%s\t%s\t%s %s\t%s\n",
    date('c'),
    $orderId,
    $order['pay_amount'] ?? '?',
    $order['currency'] ?? '?',
    json_encode($order['settlement'] ?? null, JSON_UNESCAPED_UNICODE)
);
file_put_contents(LOG_PATH, $line, FILE_APPEND | LOCK_EX);

// 这里放你真正的发货 / 开通逻辑（也要能被重复调用而不出事）
error_log("订单已支付，开始发货 {$orderId}");

respond(200, ['ok' => true]);
