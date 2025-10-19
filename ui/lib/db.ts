import Database from "better-sqlite3";
import path from "path";
import fs from "fs";

let cachedDb: Database.Database | null = null;

const defaultPath = path.resolve(process.cwd(), "../data/incidecoder.db");

function resolveDatabasePath(): string {
  const envPath = process.env.DATABASE_PATH;
  if (envPath && envPath.trim().length > 0) {
    return path.resolve(process.cwd(), envPath);
  }
  return defaultPath;
}

declare global {
  // eslint-disable-next-line no-var
  var __inciscraperDb: Database.Database | undefined;
}

function createDatabase(): Database.Database {
  const dbPath = resolveDatabasePath();
  const parentDir = path.dirname(dbPath);
  if (!fs.existsSync(parentDir)) {
    fs.mkdirSync(parentDir, { recursive: true });
  }
  if (!fs.existsSync(dbPath)) {
    fs.writeFileSync(dbPath, "");
  }
  const db = new Database(dbPath);
  db.pragma("journal_mode = WAL");
  return db;
}

export function getDatabase(): Database.Database {
  if (typeof globalThis.__inciscraperDb !== "undefined") {
    return globalThis.__inciscraperDb;
  }
  const db = cachedDb ?? createDatabase();
  if (process.env.NODE_ENV !== "production") {
    globalThis.__inciscraperDb = db;
  }
  cachedDb = db;
  return db;
}

export function listTables(): string[] {
  const db = getDatabase();
  const rows = db
    .prepare(
      "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    .all() as { name: string }[];
  return rows.map((row) => row.name);
}

export interface ColumnInfo {
  cid: number;
  name: string;
  type: string;
  notnull: number;
  dflt_value: unknown;
  pk: number;
}

export interface TableMetadata {
  columns: ColumnInfo[];
  primaryKey: string | null;
  usesRowId: boolean;
  rowCount: number;
}

export function getTableMetadata(table: string): TableMetadata | null {
  const db = getDatabase();
  const info = db.prepare(`PRAGMA table_info('${table.replace(/'/g, "''")}')`).all() as ColumnInfo[];
  if (info.length === 0) {
    return null;
  }
  const primary = info.find((col) => col.pk > 0)?.name ?? null;
  const rowCount = db
    .prepare(`SELECT COUNT(*) as count FROM '${table.replace(/'/g, "''")}'`)
    .get() as { count: number };
  return {
    columns: info,
    primaryKey: primary,
    usesRowId: primary === null,
    rowCount: rowCount?.count ?? 0,
  };
}
