import { createShopApiKey } from "@/lib/localDb";
import { verifyShopSignature } from "@/lib/shopHmac";
import { getConsistentMachineId } from "@/shared/utils/machineId";

export const dynamic = "force-dynamic";

function unauthorized() {
  return Response.json({ error: "Unauthorized" }, { status: 401 });
}

export async function POST(request) {
  const rawBody = await request.text();
  if (!verifyShopSignature(request, rawBody)) return unauthorized();

  let body;
  try {
    body = JSON.parse(rawBody);
  } catch {
    return Response.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  const shopOrderId = String(body.shopOrderId || "").trim();
  const tokenQuota = Number(body.tokenQuota);
  const allowedModels = Array.isArray(body.allowedModels)
    ? body.allowedModels.map((item) => String(item).trim()).filter(Boolean)
    : [];
  if (!/^[A-Za-z0-9._:-]{1,128}$/.test(shopOrderId)) {
    return Response.json({ error: "Invalid shopOrderId" }, { status: 400 });
  }
  if (!Number.isSafeInteger(tokenQuota) || tokenQuota <= 0) {
    return Response.json({ error: "Invalid tokenQuota" }, { status: 400 });
  }
  if (allowedModels.length === 0 || allowedModels.length > 20) {
    return Response.json({ error: "allowedModels is required" }, { status: 400 });
  }

  try {
    const machineId = await getConsistentMachineId();
    const apiKey = await createShopApiKey({
      name: String(body.name || `Shop order ${shopOrderId}`).slice(0, 160),
      machineId,
      tokenQuota,
      shopOrderId,
      telegramUserId: body.telegramUserId,
      allowedModels,
    });
    return Response.json({
      shopOrderId: apiKey.shopOrderId,
      keyId: apiKey.id,
      key: apiKey.key,
      tokenQuota: apiKey.tokenQuota,
      tokensUsed: apiKey.tokensUsed,
      remainingTokens: apiKey.remainingTokens,
      isActive: apiKey.isActive,
      disabledReason: apiKey.disabledReason,
    }, { status: 201 });
  } catch (error) {
    console.error("[Shop keys] Provision failed:", error?.message || error);
    return Response.json({ error: "Could not provision API key" }, { status: 500 });
  }
}
