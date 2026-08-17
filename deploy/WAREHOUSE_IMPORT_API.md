# Automated Warehouse Import API

This endpoint lets the automation tool import account/key stock using the same
rules as Admin -> Inventory. It encrypts every accepted item before storage and
does not return account contents.

## Endpoint

```text
POST https://token.vietshare.site/v1/warehouse/inventory/import
```

The endpoint is disabled until production has a dedicated
`WAREHOUSE_API_KEY` and `WAREHOUSE_API_ENABLED=true`. Keep that key only in the
automation tool's secret store and the VPS `.env`; never put it in frontend code
or logs. An optional `WAREHOUSE_API_ALLOWED_IPS` allowlist can restrict callers.

## Authentication

Send these headers on every request:

```text
X-Timestamp: 1784319000
X-Nonce: a-new-random-value-at-least-12-chars
X-Signature: <64 lowercase hex characters>
Idempotency-Key: inventory-import-20260817-0001
Content-Type: application/json
```

Sign the exact raw request body. The canonical string is:

```text
timestamp|nonce|METHOD|PATH_WITH_QUERY|sha256(raw_body)
```

The signature is HMAC-SHA256 using `WAREHOUSE_API_KEY` as the secret. The
timestamp window is five minutes by default. A nonce can only be used once.

```python
import hashlib
import hmac
import secrets
import time

body = b'{"product_id":7,"items":["user@example.com|password"],"cost_amount":35000}'
timestamp = str(int(time.time()))
nonce = secrets.token_hex(16)
path = "/v1/warehouse/inventory/import"
canonical = "|".join((
    timestamp,
    nonce,
    "POST",
    path,
    hashlib.sha256(body).hexdigest(),
))
signature = hmac.new(
    WAREHOUSE_API_KEY.encode(), canonical.encode(), hashlib.sha256
).hexdigest()
```

## Request body

`items` is preferred for JSON clients. For compatibility with the web form, a
single multiline `items_text` string is also accepted. Each account/key is one
line; use `---` on its own line when one item contains multiple lines.

```json
{
  "product_id": 7,
  "items": [
    "user1@example.com|password1",
    "user2@example.com|password2"
  ],
  "cost_amount": 35000,
  "import_note_id": 12,
  "new_import_note": "Nguồn ngoài · lô 2026-08-17",
  "lock_sale_price": false,
  "notify_stock_arrival": true
}
```

Fields:

- `product_id`: visible, active account product ID from Admin.
- `items` or `items_text`: account/key rows to import.
- `cost_amount`: actual cost per account; accepts an integer or formatted text
  such as `35.000`.
- `import_note_id`: reuse a note already created in Admin.
- `new_import_note`: create/reuse a note for this batch; it takes precedence over
  `import_note_id`.
- `lock_sale_price`: same price-lock behavior as the web checkbox for external
  API products.
- `notify_stock_arrival`: queue the normal stock-arrival message after a
  successful import.

## Response and safety

```json
{
  "success": true,
  "request_id": 41,
  "status": "completed",
  "product_id": 7,
  "product": "GPT Plus",
  "accepted_count": 2,
  "duplicate_count": 0,
  "cost_amount": 35000,
  "stock_before": 4,
  "stock_after": 6,
  "price_locked": false,
  "notification_queued": true
}
```

Duplicate rows are skipped and recorded in Admin's duplicate-warning table.
Items are encrypted at rest. The API never returns passwords, keys, duplicate
identifiers, supplier data, or database internals.

Retry a timeout with the **same** `Idempotency-Key` and unchanged body, but a
new timestamp, nonce, and signature. A completed request is returned without
importing rows again. Reusing the key with a different body returns
`IDEMPOTENCY_MISMATCH`.

Common errors: `401` authentication/replay, `403` IP blocked, `409` concurrent
or mismatched idempotency, `413` too many items, and `429` rate limited.
