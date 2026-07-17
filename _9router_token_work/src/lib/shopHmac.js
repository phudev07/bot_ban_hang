import crypto from "node:crypto";

const MAX_CLOCK_SKEW_SECONDS = 300;

function canonicalPayload(timestamp, method, pathname, body) {
  return `${timestamp}\n${method.toUpperCase()}\n${pathname}\n${body}`;
}

export function verifyShopSignature(request, rawBody = "") {
  const secret = process.env.SHOP_HMAC_SECRET || "";
  const timestamp = request.headers.get("x-shop-timestamp") || "";
  const signature = request.headers.get("x-shop-signature") || "";
  if (!secret || !/^\d{10}$/.test(timestamp) || !/^[a-f0-9]{64}$/i.test(signature)) {
    return false;
  }
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - Number(timestamp)) > MAX_CLOCK_SKEW_SECONDS) return false;

  const pathname = new URL(request.url).pathname;
  const expected = crypto
    .createHmac("sha256", secret)
    .update(canonicalPayload(timestamp, request.method, pathname, rawBody))
    .digest("hex");
  const suppliedBuffer = Buffer.from(signature, "hex");
  const expectedBuffer = Buffer.from(expected, "hex");
  return suppliedBuffer.length === expectedBuffer.length
    && crypto.timingSafeEqual(suppliedBuffer, expectedBuffer);
}
