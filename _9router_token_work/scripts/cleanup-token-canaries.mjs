import Database from "better-sqlite3";

const db = new Database("/var/lib/9router/db/data.sqlite");
try {
  const rows = db.prepare(
    "SELECT id FROM apiKeys WHERE shopOrderId LIKE 'CANARY-%'"
  ).all();
  const cleanup = db.transaction(() => {
    const deleteReservations = db.prepare("DELETE FROM apiKeyReservations WHERE apiKeyId = ?");
    const deleteKey = db.prepare("DELETE FROM apiKeys WHERE id = ?");
    for (const row of rows) {
      deleteReservations.run(row.id);
      deleteKey.run(row.id);
    }
  });
  cleanup();
  console.log(`Removed ${rows.length} canary keys`);
} finally {
  db.close();
}
