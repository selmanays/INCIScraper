import { NextResponse } from "next/server";
import { listTables } from "@/lib/db";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const tables = listTables();
    return NextResponse.json({ tables });
  } catch (error) {
    console.error("Schema fetch failed", error);
    return NextResponse.json(
      { error: "Tablolar alınamadı" },
      { status: 500 }
    );
  }
}
