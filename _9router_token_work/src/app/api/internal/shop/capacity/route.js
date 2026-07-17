import { getApiKeys } from "@/lib/localDb";
import { verifyShopSignature } from "@/lib/shopHmac";

export const dynamic = "force-dynamic";

function unauthorized() {
  return Response.json({ error: "Unauthorized" }, { status: 401 });
}

export async function GET(request) {
  if (!verifyShopSignature(request, "")) return unauthorized();

  const shopKeys = (await getApiKeys()).filter(
    (apiKey) => apiKey.shopOrderId && apiKey.tokenQuota != null,
  );
  const totals = shopKeys.reduce(
    (result, apiKey) => {
      result.tokenQuota += Math.max(0, Number(apiKey.tokenQuota || 0));
      result.tokensUsed += Math.max(0, Number(apiKey.tokensUsed || 0));
      result.reservedTokens += Math.max(0, Number(apiKey.reservedTokens || 0));
      result.remainingTokens += Math.max(0, Number(apiKey.remainingTokens || 0));
      result.availableTokens += Math.max(0, Number(apiKey.availableTokens || 0));
      result.activeKeys += apiKey.isActive && apiKey.remainingTokens > 0 ? 1 : 0;
      result.exhaustedKeys += apiKey.remainingTokens <= 0 ? 1 : 0;
      return result;
    },
    {
      keyCount: shopKeys.length,
      activeKeys: 0,
      exhaustedKeys: 0,
      tokenQuota: 0,
      tokensUsed: 0,
      reservedTokens: 0,
      remainingTokens: 0,
      availableTokens: 0,
    },
  );

  return Response.json({ ...totals, checkedAt: new Date().toISOString() });
}
