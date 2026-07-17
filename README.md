# Telegram SePay Shop

Bot Telegram ban san pham so, co vi noi bo, nap tien tu dong qua SePay, kho ma hoa,
giao hang tu dong va menu tieng Viet/Anh.

## Chuc nang

- `/start` hien ten Telegram, ID, username, so du va menu inline.
- Danh muc -> mat hang -> thong tin, gia, ton kho -> mua ngay.
- Nap tien bang QR SePay va webhook cong tien tu dong.
- Chong webhook lap va khoa dong khi tru tien/lay hang.
- Xem don mua, lay lai code/san pham da mua, ho so va ho tro.
- Quan tri danh muc, san pham va kho ngay trong Telegram.
- Du lieu trong kho duoc ma hoa bang Fernet truoc khi ghi vao PostgreSQL.
- Ho tro nguon hang Sumistore: gia dong bang gia nguon + markup, ton kho gioi han
  theo ca kho nguon va so du tai khoan nguon.
- Dashboard tach doanh thu, gia von API, giam gia va loi nhuan theo ngay, thang,
  nam va toan bo lich su; gia von thuc te duoc luu theo tung don.
- Ma giam gia theo tung san pham, ho tro giam so tien hoac phan tram, thoi han va
  gioi han luot su dung cho ca mua bang vi va thanh toan QR.
- API dau kho tai `https://token.vietshare.site/v1`: doi tac dong bo san pham,
  gia ban, ton kho, nap vi va dat mua tai khoan tu dong.
- Moi nick Telegram tu co mot API client khi mo muc `API dau kho`; API ID co dinh,
  API Secret co the tu doi va secret cu mat hieu luc ngay.
- Gioi thieu ban be nhan 5% so tien thuc tra cua moi don thanh cong. Hoa hong duoc
  cong vao vi mot lan cho moi ma don shop, ke ca don Telegram, QR va API.

Chi ban tai khoan ma ban co quyen phan phoi.

## API dau kho

Base URL production:

```text
https://token.vietshare.site/v1
```

Tai lieu tich hop day du:

```text
https://token.vietshare.site/docs
```

API nay danh cho shop doi tac dau noi kho tai khoan, khong phai API quan tri bot.
Tien mua hang duoc tru truc tiep tu vi Telegram cua chu API client. Gia san pham
Sumistore la gia dong: shop dong bo gia von, cong markup va luu gia von thuc te
vao tung don khi giao hang.

Endpoint:

```text
GET  /account
GET  /products
GET  /catalog
GET  /products/{product_id}
GET  /stock/{product_id}
POST /orders
GET  /orders
GET  /orders/{order_code}
POST /deposits
GET  /deposits/{deposit_code}
```

Moi request co xac thuc phai gui cac header:

```text
X-Shop-API-ID: VS...
X-Timestamp: 1752796800
X-Nonce: mot_chuoi_ngau_nhien_toi_thieu_12_ky_tu
X-Signature: hmac_sha256_hex
```

Chuoi ky:

```text
timestamp|nonce|METHOD|PATH_WITH_QUERY|sha256(raw_body)
```

Vi du Python tao chu ky:

```python
import hashlib
import hmac

body = b'{"product_id":1,"quantity":2}'
canonical = "|".join(
    (timestamp, nonce, "POST", "/v1/orders", hashlib.sha256(body).hexdigest())
)
signature = hmac.new(api_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
```

`POST /orders` bat buoc co them `Idempotency-Key` dai 8-128 ky tu. Gui lai cung
key va cung payload se tra lai don cu, khong tru tien va khong lay hang lan hai.
Khong tai su dung key do cho payload khac.

Body dat hang:

```json
{"product_id": 1, "quantity": 2, "coupon_code": "SALE10"}
```

He thong khoa dong vi, khoa ton kho local bang `FOR UPDATE SKIP LOCKED`, khoa
luong mua Sumistore va co unique idempotency trong PostgreSQL. Vi vay nhieu doi
tac dat hang hoac thanh toan cung luc khong duoc ban trung tai khoan hay tru tien
lap.

## Cai tren Ubuntu 22.04

### 1. Cai Docker

```bash
sudo apt update
sudo apt install -y ca-certificates curl
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker
```

### 2. Cau hinh

```bash
cp .env.example .env
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
nano .env
```

Sua toi thieu cac bien:

- `BOT_TOKEN`: token tu BotFather.
- `ADMIN_IDS`: Telegram ID cua quan tri vien.
- `COMMUNITY_GROUP_URL`: link nhom Telegram cua cua hang; de trong neu chua co.
- `SEPAY_ENABLED=false`: giu nguyen khi chi muon chay thu Telegram.
- `SEPAY_AUTH_MODE=hmac`: dung HMAC-SHA256 theo khuyen nghi cua SePay.
- `SEPAY_WEBHOOK_SECRET`: secret HMAC trung voi cau hinh webhook tren SePay.
- `PAYMENT_PREFIX=NAP`: tien to ma thanh toan dung de loc giao dich.
- `BANK_CODE`, `BANK_ACCOUNT`, `BANK_ACCOUNT_NAME`.
- `INVENTORY_ENCRYPTION_KEY`: key vua tao; mat key nay se khong doc lai duoc kho.
- `SUMISTORE_API_ID`: API ID rieng lay trong bot Sumistore; khong dua len dashboard/log.
- `SUMISTORE_MARKUP=5000`: muc cong mac dinh khi tao san pham nguon lan dau;
  markup rieng da sua tren dashboard se duoc giu nguyen khi bot khoi dong lai.
- Mat khau PostgreSQL trong ca `.env` va `docker-compose.yml` phai trung nhau.

### 3. Chay bot

```bash
docker compose up -d --build
docker compose logs -f app
```

Kiem tra API noi bo:

```bash
curl http://127.0.0.1:8080/health
```

## HTTPS va webhook SePay

Tro mot ten mien, vi du `bot.example.com`, ve IP VPS. Cai Nginx va Certbot:

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo tee /etc/nginx/sites-available/telegram-shop >/dev/null <<'NGINX'
server {
    server_name bot.example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGINX
sudo ln -s /etc/nginx/sites-available/telegram-shop /etc/nginx/sites-enabled/telegram-shop
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d bot.example.com
```

Trong SePay, tao webhook:

- URL: `https://bot.example.com/webhooks/sepay`
- Method: `POST`
- Authentication: chon `HMAC-SHA256` va dung gia tri `SEPAY_WEBHOOK_SECRET`.
- Sau khi dien du thong tin ngan hang va webhook, dat `SEPAY_ENABLED=true` roi
  chay `docker compose up -d` de bat nap tien.
- Bot xac minh `X-SePay-Signature: sha256=<hex>` tren chuoi
  `{X-SePay-Timestamp}.{raw_body}` va tu choi timestamp lech qua 5 phut.

Payload duoc ho tro co cac truong SePay thong dung: `id`, `transferType`,
`transferAmount`, `content`, `code`, `description`, `referenceCode`. Giao dich chi
duoc cong tien neu noi dung co dung ma `NAP...` da duoc bot tao. Moi ma giao dich
ngan hang chi duoc cong mot lan; neu khach vo tinh chuyen hai giao dich rieng cung
mot noi dung thi ca hai van duoc ghi nhan.

## Lenh quan tri Telegram

```text
/admin
/products
/addcategory Tài khoản
/addproduct 1 | ChatGPT mẫu | 99000 | Mô tả sản phẩm
/addstock 1
username:password
---
username2:password2
```

Moi khoi hang trong `/addstock` duoc ngan bang mot dong chi co `---`. Khong dua
file `.env`, encryption key, token bot hoac master key cho nguoi khac.

## Sao luu

```bash
docker compose exec -T postgres pg_dump -U shop shop | gzip > shop-$(date +%F).sql.gz
```

Nen chay lenh sao luu bang cron moi ngay va luu them mot ban ngoai VPS.

## Kiem thu local

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
```
