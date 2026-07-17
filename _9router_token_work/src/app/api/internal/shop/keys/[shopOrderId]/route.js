import { getShopApiKeyByOrderId, revokeShopApiKey } from "@/lib/localDb";
import { verifyShopSignature } from "@/lib/shopHmac";

export const dynamic = "force-dynamic";

function unauthorized() {
  return Response.json({ error: "Unauthorized" }, { status: 401 });
}

function keyPayload(apiKey) {
  return {
    shopOrderId: apiKey.shopOrderId,
    keyId: apiKey.id,
    key: apiKey.key,
    tokenQuota: apiKey.tokenQuota,
    tokensUsed: apiKey.tokensUsed,
    reservedTokens: apiKey.reservedTokens,
    remainingTokens: apiKey.remainingTokens,
    availableTokens: apiKey.availableTokens,
    isActive: apiKey.isActive,
    disabledReason: apiKey.disabledReason,
    allowedModels: apiKey.allowedModels,
    createdAt: apiKey.createdAt,
    updatedAt: apiKey.updatedAt,
  };
}

export async function GET(request, { params }) {
  if (!verifyShopSignature(request, "")) return unauthorized();
  const { shopOrderId } = await params;
  const apiKey = await getShopApiKeyByOrderId(shopOrderId);
  if (!apiKey) return Response.json({ error: "Key not found" }, { status: 404 });
  return Response.json(keyPayload(apiKey));
}

export async function DELETE(request, { params }) {
  const rawBody = await request.text();
  if (!verifyShopSignature(request, rawBody)) return unauthorized();
  const { shopOrderId } = await params;
  let reason = "shop_revoked";
  if (rawBody) {
    try {
      reason = String(JSON.parse(rawBody).reason || reason).slice(0, 120);
    } catch {
      return Response.json({ error: "Invalid JSON body" }, { status: 400 });
    }
  }
  const revoked = await revokeShopApiKey(shopOrderId, reason);
  if (!revoked) return Response.json({ error: "Key not found" }, { status: 404 });
  const apiKey = await getShopApiKeyByOrderId(shopOrderId);
  return Response.json(keyPayload(apiKey));
}
