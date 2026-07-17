import { v4 as uuidv4 } from "uuid";
import { getAdapter } from "../driver.js";

const DEFAULT_RESERVATION_TTL_MS = 60 * 60 * 1000;

function parseAllowedModels(value) {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.filter((item) => typeof item === "string") : null;
  } catch {
    return null;
  }
}

function rowToKey(row) {
  if (!row) return null;
  const tokenQuota = row.tokenQuota == null ? null : Number(row.tokenQuota);
  const tokensUsed = Number(row.tokensUsed || 0);
  const reservedTokens = Number(row.reservedTokens || 0);
  return {
    id: row.id,
    key: row.key,
    name: row.name,
    machineId: row.machineId,
    isActive: row.isActive === 1 || row.isActive === true,
    tokenQuota,
    tokensUsed,
    reservedTokens,
    remainingTokens: tokenQuota == null ? null : Math.max(0, tokenQuota - tokensUsed),
    availableTokens: tokenQuota == null
      ? null
      : Math.max(0, tokenQuota - tokensUsed - reservedTokens),
    shopOrderId: row.shopOrderId || null,
    telegramUserId: row.telegramUserId || null,
    allowedModels: parseAllowedModels(row.allowedModels),
    disabledReason: row.disabledReason || null,
    createdAt: row.createdAt,
    updatedAt: row.updatedAt || row.createdAt,
  };
}

function modelMatches(model, patterns) {
  if (!Array.isArray(patterns) || patterns.length === 0) return false;
  return patterns.some((pattern) => {
    const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replaceAll("*", ".*");
    return new RegExp(`^${escaped}$`, "i").test(model);
  });
}

function cleanupExpiredReservations(db, nowIso) {
  const expired = db.all(
    `SELECT id, apiKeyId, reservedTokens FROM apiKeyReservations
     WHERE status = 'reserved' AND expiresAt <= ?`,
    [nowIso]
  );
  for (const reservation of expired) {
    db.run(
      `UPDATE apiKeys
       SET reservedTokens = MAX(0, reservedTokens - ?), updatedAt = ?
       WHERE id = ?`,
      [Number(reservation.reservedTokens || 0), nowIso, reservation.apiKeyId]
    );
    db.run(
      `UPDATE apiKeyReservations SET status = 'expired', settledAt = ? WHERE id = ?`,
      [nowIso, reservation.id]
    );
  }
}

export async function getApiKeys() {
  const db = await getAdapter();
  const rows = db.all(`SELECT * FROM apiKeys ORDER BY createdAt ASC`);
  return rows.map(rowToKey);
}

export async function getApiKeyById(id) {
  const db = await getAdapter();
  return rowToKey(db.get(`SELECT * FROM apiKeys WHERE id = ?`, [id]));
}

export async function getApiKeyByValue(key) {
  const db = await getAdapter();
  return rowToKey(db.get(`SELECT * FROM apiKeys WHERE key = ?`, [key]));
}

export async function getShopApiKeyByOrderId(shopOrderId) {
  const db = await getAdapter();
  return rowToKey(db.get(`SELECT * FROM apiKeys WHERE shopOrderId = ?`, [shopOrderId]));
}

export async function createApiKey(name, machineId) {
  if (!machineId) throw new Error("machineId is required");
  const db = await getAdapter();
  const { generateApiKeyWithMachine } = await import("@/shared/utils/apiKey");
  const result = generateApiKeyWithMachine(machineId);
  const now = new Date().toISOString();
  const apiKey = {
    id: uuidv4(),
    name,
    key: result.key,
    machineId,
    isActive: true,
    tokenQuota: null,
    tokensUsed: 0,
    reservedTokens: 0,
    shopOrderId: null,
    telegramUserId: null,
    allowedModels: null,
    disabledReason: null,
    createdAt: now,
    updatedAt: now,
  };
  db.run(
    `INSERT INTO apiKeys(
      id, key, name, machineId, isActive, tokenQuota, tokensUsed, reservedTokens,
      shopOrderId, telegramUserId, allowedModels, disabledReason, createdAt, updatedAt
    ) VALUES(?, ?, ?, ?, ?, NULL, 0, 0, NULL, NULL, NULL, NULL, ?, ?)`,
    [apiKey.id, apiKey.key, apiKey.name, apiKey.machineId, 1, now, now]
  );
  return apiKey;
}

export async function createShopApiKey({
  name,
  machineId,
  tokenQuota,
  shopOrderId,
  telegramUserId,
  allowedModels,
}) {
  if (!machineId) throw new Error("machineId is required");
  if (!shopOrderId) throw new Error("shopOrderId is required");
  if (!Number.isSafeInteger(tokenQuota) || tokenQuota <= 0) {
    throw new Error("tokenQuota must be a positive safe integer");
  }
  if (!Array.isArray(allowedModels) || allowedModels.length === 0) {
    throw new Error("allowedModels is required");
  }

  const db = await getAdapter();
  const existing = db.get(`SELECT * FROM apiKeys WHERE shopOrderId = ?`, [shopOrderId]);
  if (existing) return rowToKey(existing);

  const { generateApiKeyWithMachine } = await import("@/shared/utils/apiKey");
  const generated = generateApiKeyWithMachine(machineId);
  const now = new Date().toISOString();
  const id = uuidv4();
  try {
    db.run(
      `INSERT INTO apiKeys(
        id, key, name, machineId, isActive, tokenQuota, tokensUsed, reservedTokens,
        shopOrderId, telegramUserId, allowedModels, disabledReason, createdAt, updatedAt
      ) VALUES(?, ?, ?, ?, 1, ?, 0, 0, ?, ?, ?, NULL, ?, ?)`,
      [
        id,
        generated.key,
        name || `Shop order ${shopOrderId}`,
        machineId,
        tokenQuota,
        shopOrderId,
        telegramUserId == null ? null : String(telegramUserId),
        JSON.stringify(allowedModels),
        now,
        now,
      ]
    );
  } catch (error) {
    const raced = db.get(`SELECT * FROM apiKeys WHERE shopOrderId = ?`, [shopOrderId]);
    if (raced) return rowToKey(raced);
    throw error;
  }
  return rowToKey(db.get(`SELECT * FROM apiKeys WHERE id = ?`, [id]));
}

export async function updateApiKey(id, data) {
  const db = await getAdapter();
  let result = null;
  db.transaction(() => {
    const row = db.get(`SELECT * FROM apiKeys WHERE id = ?`, [id]);
    if (!row) return;
    const current = rowToKey(row);
    const merged = { ...current, ...data };
    const now = new Date().toISOString();
    db.run(
      `UPDATE apiKeys SET
        key = ?, name = ?, machineId = ?, isActive = ?, tokenQuota = ?, tokensUsed = ?,
        reservedTokens = ?, shopOrderId = ?, telegramUserId = ?, allowedModels = ?,
        disabledReason = ?, updatedAt = ?
       WHERE id = ?`,
      [
        merged.key,
        merged.name,
        merged.machineId,
        merged.isActive ? 1 : 0,
        merged.tokenQuota,
        merged.tokensUsed || 0,
        merged.reservedTokens || 0,
        merged.shopOrderId,
        merged.telegramUserId,
        merged.allowedModels ? JSON.stringify(merged.allowedModels) : null,
        merged.disabledReason,
        now,
        id,
      ]
    );
    result = rowToKey(db.get(`SELECT * FROM apiKeys WHERE id = ?`, [id]));
  });
  return result;
}

export async function deleteApiKey(id) {
  const db = await getAdapter();
  let deleted = false;
  db.transaction(() => {
    db.run(`DELETE FROM apiKeyReservations WHERE apiKeyId = ?`, [id]);
    const res = db.run(`DELETE FROM apiKeys WHERE id = ?`, [id]);
    deleted = (res?.changes ?? 0) > 0;
  });
  return deleted;
}

export async function validateApiKey(key, { allowMetered = false } = {}) {
  const db = await getAdapter();
  const row = db.get(
    `SELECT isActive, tokenQuota, tokensUsed FROM apiKeys WHERE key = ?`,
    [key]
  );
  if (!row || !(row.isActive === 1 || row.isActive === true)) return false;
  if (row.tokenQuota == null) return true;
  if (!allowMetered) return false;
  return Number(row.tokensUsed || 0) < Number(row.tokenQuota);
}

export async function reserveApiKeyQuota({
  key,
  model,
  inputEstimate,
  minimumInputEstimate,
  requestedOutput,
  ttlMs = DEFAULT_RESERVATION_TTL_MS,
}) {
  const db = await getAdapter();
  const now = new Date();
  const nowIso = now.toISOString();
  let result = { ok: false, reason: "invalid_key" };

  db.transaction(() => {
    cleanupExpiredReservations(db, nowIso);
    const row = db.get(`SELECT * FROM apiKeys WHERE key = ?`, [key]);
    if (!row || !(row.isActive === 1 || row.isActive === true)) {
      result = { ok: false, reason: row?.disabledReason || "invalid_key" };
      return;
    }
    if (row.tokenQuota == null) {
      result = { ok: true, metered: false, key: rowToKey(row) };
      return;
    }

    const patterns = parseAllowedModels(row.allowedModels);
    if (!modelMatches(model, patterns)) {
      result = { ok: false, reason: "model_not_allowed" };
      return;
    }

    const quota = Number(row.tokenQuota);
    const used = Number(row.tokensUsed || 0);
    const reserved = Number(row.reservedTokens || 0);
    const available = Math.max(0, quota - used - reserved);
    const safeInput = Math.max(1, Math.floor(Number(inputEstimate) || 0));
    const minimumInput = Math.max(1, Math.floor(Number(minimumInputEstimate) || 0));
    if (available <= minimumInput) {
      if (quota - used <= 0) {
        db.run(
          `UPDATE apiKeys SET isActive = 0, disabledReason = 'quota_exhausted', updatedAt = ? WHERE id = ?`,
          [nowIso, row.id]
        );
      }
      result = { ok: false, reason: "insufficient_quota", remainingTokens: Math.max(0, quota - used) };
      return;
    }
    const outputAvailable = available - safeInput;
    const terminalReservation = outputAvailable < 1;
    const outputTokens = terminalReservation
      ? 1
      : Math.max(1, Math.min(Math.floor(Number(requestedOutput) || 0), outputAvailable));
    const reserveTokens = terminalReservation ? available : safeInput + outputTokens;
    const reservationId = uuidv4();
    const expiresAt = new Date(now.getTime() + ttlMs).toISOString();
    db.run(
      `INSERT INTO apiKeyReservations(
        id, apiKeyId, reservedTokens, actualTokens, status, createdAt, expiresAt, settledAt
      ) VALUES(?, ?, ?, NULL, 'reserved', ?, ?, NULL)`,
      [reservationId, row.id, reserveTokens, nowIso, expiresAt]
    );
    db.run(
      `UPDATE apiKeys SET reservedTokens = reservedTokens + ?, updatedAt = ? WHERE id = ?`,
      [reserveTokens, nowIso, row.id]
    );
    result = {
      ok: true,
      metered: true,
      reservationId,
      outputTokens,
      reservedTokens: reserveTokens,
      remainingTokens: Math.max(0, quota - used),
    };
  });
  return result;
}

export async function settleApiKeyReservation(reservationId, actualTokens = null) {
  if (!reservationId) return null;
  const db = await getAdapter();
  const now = new Date().toISOString();
  let result = null;
  db.transaction(() => {
    const reservation = db.get(
      `SELECT * FROM apiKeyReservations WHERE id = ?`,
      [reservationId]
    );
    if (!reservation || reservation.status !== "reserved") return;
    const keyRow = db.get(`SELECT * FROM apiKeys WHERE id = ?`, [reservation.apiKeyId]);
    if (!keyRow) return;

    const reserved = Number(reservation.reservedTokens || 0);
    const requestedActual = Number(actualTokens);
    const charged = actualTokens == null || !Number.isFinite(requestedActual) || requestedActual <= 0
      ? reserved
      : Math.max(0, Math.floor(requestedActual));
    const quota = Number(keyRow.tokenQuota || 0);
    const newUsed = Math.min(quota, Number(keyRow.tokensUsed || 0) + charged);
    const exhausted = newUsed >= quota;
    db.run(
      `UPDATE apiKeys SET
        tokensUsed = ?,
        reservedTokens = MAX(0, reservedTokens - ?),
        isActive = CASE WHEN ? THEN 0 ELSE isActive END,
        disabledReason = CASE WHEN ? THEN 'quota_exhausted' ELSE disabledReason END,
        updatedAt = ?
       WHERE id = ?`,
      [newUsed, reserved, exhausted ? 1 : 0, exhausted ? 1 : 0, now, keyRow.id]
    );
    db.run(
      `UPDATE apiKeyReservations
       SET actualTokens = ?, status = 'settled', settledAt = ? WHERE id = ?`,
      [charged, now, reservationId]
    );
    result = rowToKey(db.get(`SELECT * FROM apiKeys WHERE id = ?`, [keyRow.id]));
  });
  return result;
}

export async function releaseApiKeyReservation(reservationId) {
  if (!reservationId) return false;
  const db = await getAdapter();
  const now = new Date().toISOString();
  let released = false;
  db.transaction(() => {
    const reservation = db.get(
      `SELECT * FROM apiKeyReservations WHERE id = ?`,
      [reservationId]
    );
    if (!reservation || reservation.status !== "reserved") return;
    db.run(
      `UPDATE apiKeys
       SET reservedTokens = MAX(0, reservedTokens - ?), updatedAt = ? WHERE id = ?`,
      [Number(reservation.reservedTokens || 0), now, reservation.apiKeyId]
    );
    db.run(
      `UPDATE apiKeyReservations SET status = 'released', settledAt = ? WHERE id = ?`,
      [now, reservationId]
    );
    released = true;
  });
  return released;
}

export async function revokeShopApiKey(shopOrderId, reason = "shop_revoked") {
  const db = await getAdapter();
  const now = new Date().toISOString();
  let revoked = false;
  db.transaction(() => {
    const row = db.get(`SELECT id FROM apiKeys WHERE shopOrderId = ?`, [shopOrderId]);
    if (!row) return;
    db.run(
      `UPDATE apiKeyReservations
       SET status = 'revoked', settledAt = ?
       WHERE apiKeyId = ? AND status = 'reserved'`,
      [now, row.id]
    );
    const res = db.run(
      `UPDATE apiKeys
       SET isActive = 0, reservedTokens = 0, disabledReason = ?, updatedAt = ?
       WHERE id = ?`,
      [reason, now, row.id]
    );
    revoked = (res?.changes ?? 0) > 0;
  });
  return revoked;
}
