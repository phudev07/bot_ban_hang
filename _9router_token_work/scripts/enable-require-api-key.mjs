import Database from "better-sqlite3";

const dbPath = process.env.NINE_ROUTER_DB || "/var/lib/9router/db/data.sqlite";
const db = new Database(dbPath);
try {
  const row = db.prepare("SELECT data FROM settings WHERE id = 1").get();
  const settings = row?.data ? JSON.parse(row.data) : {};
  settings.requireApiKey = true;
  db.prepare(
    "INSERT INTO settings(id, data) VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data"
  ).run(JSON.stringify(settings));
  console.log("requireApiKey enabled");
} finally {
  db.close();
}
