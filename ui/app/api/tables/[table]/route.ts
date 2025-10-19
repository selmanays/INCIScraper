import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { getDatabase, getTableMetadata } from "@/lib/db";

export const dynamic = "force-dynamic";

const updateSchema = z.object({
  updates: z
    .array(
      z.object({
        key: z.union([z.string(), z.number()]),
        data: z.record(z.any()),
      })
    )
    .default([]),
});

function sanitizeTableName(table: string): string | null {
  if (/^[A-Za-z0-9_]+$/.test(table)) {
    return table;
  }
  return null;
}

function normalizeValue(value: unknown, columnType: string | undefined): unknown {
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed.toUpperCase() === "NULL") {
      return null;
    }
    const lower = (columnType ?? "").toLowerCase();
    if (lower.includes("int")) {
      const parsed = Number(trimmed);
      if (!Number.isNaN(parsed)) {
        return parsed;
      }
    }
    if (lower.includes("real") || lower.includes("floa") || lower.includes("doub")) {
      const parsed = Number(trimmed);
      if (!Number.isNaN(parsed)) {
        return parsed;
      }
    }
    if (lower.includes("bool")) {
      if (["1", "true", "on", "yes"].includes(trimmed.toLowerCase())) {
        return 1;
      }
      if (["0", "false", "off", "no"].includes(trimmed.toLowerCase())) {
        return 0;
      }
    }
    if (trimmed === "" && lower && !lower.includes("char") && !lower.includes("text") && !lower.includes("clob")) {
      return null;
    }
    return value;
  }
  return value;
}

export async function GET(
  _req: NextRequest,
  { params }: { params: { table: string } }
) {
  const table = sanitizeTableName(params.table);
  if (!table) {
    return NextResponse.json({ error: "Tablo adı geçersiz" }, { status: 400 });
  }

  const searchParams = _req.nextUrl.searchParams;
  const limit = Math.max(1, Math.min(500, Number(searchParams.get("limit")) || 50));
  const offset = Math.max(0, Number(searchParams.get("offset")) || 0);

  const metadata = getTableMetadata(table);
  if (!metadata) {
    return NextResponse.json({ error: "Tablo bulunamadı" }, { status: 404 });
  }

  const db = getDatabase();
  const selectPrefix = metadata.usesRowId ? "rowid as __rowid__, " : "";
  const query = `SELECT ${selectPrefix} * FROM "${table}" ORDER BY ${
    metadata.primaryKey ? `"${metadata.primaryKey}"` : "rowid"
  } LIMIT ? OFFSET ?`;
  const rows = db.prepare(query).all(limit, offset) as Record<string, unknown>[];

  return NextResponse.json({
    meta: {
      columns: metadata.columns,
      primaryKey: metadata.primaryKey ?? "__rowid__",
      usesRowId: metadata.usesRowId,
      rowCount: metadata.rowCount,
      limit,
      offset,
    },
    rows,
  });
}

export async function POST(req: NextRequest, { params }: { params: { table: string } }) {
  const table = sanitizeTableName(params.table);
  if (!table) {
    return NextResponse.json({ error: "Tablo adı geçersiz" }, { status: 400 });
  }

  const metadata = getTableMetadata(table);
  if (!metadata) {
    return NextResponse.json({ error: "Tablo bulunamadı" }, { status: 404 });
  }

  const payload = updateSchema.safeParse(await req.json());
  if (!payload.success) {
    return NextResponse.json(
      { error: "Geçersiz istek", details: payload.error.flatten() },
      { status: 400 }
    );
  }

  const primaryKey = metadata.primaryKey ?? "__rowid__";
  if (!payload.data.updates.length) {
    return NextResponse.json({ updated: 0 });
  }

  const db = getDatabase();
  const columnMap = new Map(metadata.columns.map((col) => [col.name, col]));

  try {
    const runTransaction = db.transaction((updates: typeof payload.data.updates) => {
      let total = 0;
      for (const update of updates) {
        const entries = Object.entries(update.data).filter(([name]) => name !== primaryKey && columnMap.has(name));
        if (entries.length === 0) {
          continue;
        }
        const setters = entries.map(([name]) => `"${name}" = ?`).join(", ");
        const values = entries.map(([name, value]) =>
          normalizeValue(value, columnMap.get(name)?.type)
        );
        const identifierColumn = primaryKey === "__rowid__" ? "rowid" : `"${primaryKey}"`;
        const identifier = normalizeValue(
          update.key,
          primaryKey === "__rowid__" ? "INTEGER" : columnMap.get(primaryKey)?.type
        );
        const result = db
          .prepare(`UPDATE "${table}" SET ${setters} WHERE ${identifierColumn} = ?`)
          .run(...values, identifier);
        total += result.changes ?? 0;
      }
      return total;
    });

    const changes = runTransaction(payload.data.updates);
    return NextResponse.json({ updated: changes });
  } catch (error) {
    console.error("Update failed", error);
    return NextResponse.json({ error: "Kayıt güncellenemedi" }, { status: 500 });
  }
}
