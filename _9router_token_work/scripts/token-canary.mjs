import crypto from "node:crypto";
import fs from "node:fs";
import Database from "better-sqlite3";

function loadEnv(file) {
  const values = {};
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const match = line.match(/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/);
    if (match) values[match[1]] = match[2];
  }
  return values;
}

const env = loadEnv("/root/9router/.env");
const secret = env.SHOP_HMAC_SECRET;
if (!secret) throw new Error("SHOP_HMAC_SECRET is missing");
const origin = "http://127.0.0.1:20128";
const canaryModel = process.env.CANARY_MODEL || "cx/gpt-5.5";

function signedHeaders(method, path, body = "") {
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = crypto
    .createHmac("sha256", secret)
    .update(`${timestamp}\n${method}\n${path}\n${body}`)
    .digest("hex");
  return {
    "content-type": "application/json",
    "x-shop-timestamp": timestamp,
    "x-shop-signature": signature,
  };
}

async function provision(shopOrderId, tokenQuota) {
  const path = "/api/internal/shop/keys";
  const body = JSON.stringify({
    shopOrderId,
    telegramUserId: "canary",
    tokenQuota,
    allowedModels: ["gpt-*", "*/gpt-*"],
    name: "Token canary",
  });
  const response = await fetch(`${origin}${path}`, {
    method: "POST",
    headers: signedHeaders("POST", path, body),
    body,
  });
  const payload = await response.json();
  if (response.status !== 201 || !payload.key?.startsWith("sk-")) {
    throw new Error(`Provision failed with status ${response.status}`);
  }
  return payload;
}

async function status(shopOrderId) {
  const path = `/api/internal/shop/keys/${encodeURIComponent(shopOrderId)}`;
  const response = await fetch(`${origin}${path}`, {
    headers: signedHeaders("GET", path),
  });
  return { httpStatus: response.status, payload: await response.json() };
}

async function revoke(shopOrderId) {
  const path = `/api/internal/shop/keys/${encodeURIComponent(shopOrderId)}`;
  const body = JSON.stringify({ reason: "canary_complete" });
  const response = await fetch(`${origin}${path}`, {
    method: "DELETE",
    headers: signedHeaders("DELETE", path, body),
    body,
  });
  return response.status;
}

const canaryId = `CANARY-${Date.now()}`;
const tinyId = `CANARY-TINY-${Date.now()}`;
const terminalId = `CANARY-TERMINAL-${Date.now()}`;
const provisioned = await provision(canaryId, 20000);
const duplicate = await provision(canaryId, 20000);
if (duplicate.keyId !== provisioned.keyId) throw new Error("Idempotency check failed");

const chatResponse = await fetch(`${origin}/v1/chat/completions`, {
  method: "POST",
  headers: {
    "content-type": "application/json",
    authorization: `Bearer ${provisioned.key}`,
  },
  body: JSON.stringify({
    model: canaryModel,
    stream: false,
    max_completion_tokens: 5,
    messages: [{ role: "user", content: "Reply OK" }],
  }),
});
await chatResponse.text();
const afterChat = await status(canaryId);

const tiny = await provision(tinyId, 10);
const tinyResponse = await fetch(`${origin}/v1/chat/completions`, {
  method: "POST",
  headers: {
    "content-type": "application/json",
    authorization: `Bearer ${tiny.key}`,
  },
  body: JSON.stringify({
    model: canaryModel,
    stream: false,
    messages: [{ role: "user", content: "This request must be rejected before upstream" }],
  }),
});
const tinyPayload = await tinyResponse.json();

const terminal = await provision(terminalId, 500);
const terminalResponse = await fetch(`${origin}/v1/chat/completions`, {
  method: "POST",
  headers: {
    "content-type": "application/json",
    authorization: `Bearer ${terminal.key}`,
  },
  body: JSON.stringify({
    model: canaryModel,
    stream: false,
    messages: [{ role: "user", content: "Reply OK" }],
  }),
});
const terminalText = await terminalResponse.text();
let terminalPayload = null;
try {
  terminalPayload = JSON.parse(terminalText);
} catch {
  terminalPayload = terminalText;
}
const terminalStatus = await status(terminalId);

await revoke(canaryId);
await revoke(tinyId);
await revoke(terminalId);

const db = new Database("/var/lib/9router/db/data.sqlite", { readonly: true });
const unlimited = db.prepare("SELECT COUNT(*) AS count FROM apiKeys WHERE tokenQuota IS NULL").get();
const columns = db.prepare("PRAGMA table_info(apiKeys)").all().map((row) => row.name);
db.close();

console.log(JSON.stringify({
  canaryModel,
  provisionStatus: 201,
  duplicateIdempotent: true,
  chatStatus: chatResponse.status,
  statusHttp: afterChat.httpStatus,
  tokensUsed: afterChat.payload.tokensUsed,
  remainingTokens: afterChat.payload.remainingTokens,
  tinyQuotaStatus: tinyResponse.status,
  tinyQuotaCode: tinyPayload?.error?.code,
  terminalStatus: terminalResponse.status,
  terminalError: terminalResponse.ok ? null : terminalPayload?.error || terminalPayload,
  terminalRemaining: terminalStatus.payload.remainingTokens,
  unlimitedKeysPreserved: unlimited.count,
  quotaColumnsPresent: ["tokenQuota", "tokensUsed", "reservedTokens", "shopOrderId"].every(
    (column) => columns.includes(column)
  ),
}, null, 2));
