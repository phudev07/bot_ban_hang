import {
  getApiKeyByValue,
  releaseApiKeyReservation,
  reserveApiKeyQuota,
  settleApiKeyReservation,
} from "@/lib/localDb";

const DEFAULT_OUTPUT_TOKENS = 4096;
const MAX_OUTPUT_TOKENS = 64000;
const PROVIDER_INPUT_OVERHEAD_TOKENS = 8192;
const METERED_ENDPOINTS = new Set([
  "/v1/chat/completions",
  "/v1/responses",
  "/v1/messages",
]);

function requestedOutputTokens(body) {
  const value = body.max_output_tokens ?? body.max_completion_tokens ?? body.max_tokens;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return DEFAULT_OUTPUT_TOKENS;
  return Math.min(MAX_OUTPUT_TOKENS, Math.max(1, Math.floor(parsed)));
}

function clampOutputTokens(body, endpoint, outputTokens) {
  const nextBody = { ...body };
  if (endpoint === "/v1/responses") {
    nextBody.max_output_tokens = outputTokens;
  } else if (endpoint === "/v1/messages") {
    nextBody.max_tokens = outputTokens;
  } else if (body.max_tokens !== undefined && body.max_completion_tokens === undefined) {
    nextBody.max_tokens = outputTokens;
  } else {
    nextBody.max_completion_tokens = outputTokens;
  }
  return nextBody;
}

function inputTokenBounds(body) {
  const clientInput = Math.max(1, Buffer.byteLength(JSON.stringify(body), "utf8"));
  return {
    minimumInputEstimate: clientInput,
    inputEstimate: clientInput + PROVIDER_INPUT_OVERHEAD_TOKENS,
  };
}

export function quotaErrorResponse(status, code, message) {
  return new Response(
    JSON.stringify({
      error: {
        message,
        type: code === "insufficient_quota" ? "insufficient_quota" : "invalid_request_error",
        code,
      },
    }),
    {
      status,
      headers: {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
      },
    }
  );
}

export async function prepareChatQuota({ apiKey, body, endpoint, model, isCombo = false }) {
  if (!apiKey) return { ok: true, metered: false, body };
  const keyRecord = await getApiKeyByValue(apiKey);
  if (!keyRecord) {
    return { ok: false, response: quotaErrorResponse(401, "invalid_api_key", "Invalid API key") };
  }
  if (keyRecord.tokenQuota == null) return { ok: true, metered: false, body };
  if (!keyRecord.isActive || keyRecord.remainingTokens <= 0) {
    return {
      ok: false,
      response: quotaErrorResponse(429, "insufficient_quota", "This API key has no tokens remaining"),
    };
  }
  if (!METERED_ENDPOINTS.has(endpoint)) {
    return {
      ok: false,
      response: quotaErrorResponse(403, "endpoint_not_allowed", "This API key only supports GPT text endpoints"),
    };
  }
  if (isCombo) {
    return {
      ok: false,
      response: quotaErrorResponse(403, "model_not_allowed", "Combo models are not available for metered API keys"),
    };
  }

  const inputBounds = inputTokenBounds(body);
  const reservation = await reserveApiKeyQuota({
    key: apiKey,
    model,
    ...inputBounds,
    requestedOutput: requestedOutputTokens(body),
  });
  if (!reservation.ok) {
    if (reservation.reason === "model_not_allowed") {
      return {
        ok: false,
        response: quotaErrorResponse(403, "model_not_allowed", "This API key only supports the purchased GPT models"),
      };
    }
    if (reservation.reason === "insufficient_quota" || reservation.reason === "quota_exhausted") {
      return {
        ok: false,
        response: quotaErrorResponse(429, "insufficient_quota", "The remaining token quota is too small for this request"),
      };
    }
    return { ok: false, response: quotaErrorResponse(401, "invalid_api_key", "Invalid API key") };
  }
  if (!reservation.metered) return { ok: true, metered: false, body };
  return {
    ok: true,
    metered: true,
    reservationId: reservation.reservationId,
    body: clampOutputTokens(body, endpoint, reservation.outputTokens),
  };
}

export async function releaseFailedQuota(reservationId, response) {
  if (reservationId && (!response || response.status >= 400)) {
    await releaseApiKeyReservation(reservationId);
  }
  return response;
}

export async function chargeDisconnectedQuota(reservationId) {
  if (reservationId) await settleApiKeyReservation(reservationId, null);
}
