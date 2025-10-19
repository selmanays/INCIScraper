"use client";

import { useEffect, useMemo, useState } from "react";
import { DatabaseIcon, Loader2, RefreshCcw, Save } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

interface ColumnInfo {
  cid: number;
  name: string;
  type: string;
  notnull: number;
  dflt_value: unknown;
  pk: number;
}

interface TableMeta {
  columns: ColumnInfo[];
  primaryKey: string;
  usesRowId: boolean;
  rowCount: number;
  limit: number;
  offset: number;
}

type Row = Record<string, unknown> & {
  __rowid__?: number;
};

interface TableResponse {
  meta: TableMeta;
  rows: Row[];
}

interface StatusMessage {
  type: "idle" | "loading" | "success" | "error";
  message?: string;
}

const DEFAULT_LIMIT = 50;

export default function HomePage() {
  const [tables, setTables] = useState<string[]>([]);
  const [selectedTable, setSelectedTable] = useState<string>("");
  const [tableData, setTableData] = useState<TableResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [status, setStatus] = useState<StatusMessage>({ type: "idle" });
  const [editedRows, setEditedRows] = useState<Map<string | number, Row>>(new Map());
  const [limit, setLimit] = useState(DEFAULT_LIMIT);
  const [offset, setOffset] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    fetch("/api/schema")
      .then((res) => res.json())
      .then((data) => {
        if (Array.isArray(data.tables)) {
          setTables(data.tables);
          if (data.tables.length > 0) {
            setSelectedTable((current) => current || data.tables[0]);
          }
        }
      })
      .catch(() => {
        setStatus({ type: "error", message: "Tablo listesi alınamadı" });
      });
  }, []);

  useEffect(() => {
    if (!selectedTable) {
      setTableData(null);
      return;
    }
    setIsLoading(true);
    setStatus({ type: "loading", message: "Veriler yükleniyor..." });
    fetch(`/api/tables/${selectedTable}?limit=${limit}&offset=${offset}`)
      .then(async (res) => {
        if (!res.ok) {
          const error = await res.json().catch(() => ({}));
          throw new Error(error?.error || "Veriler alınamadı");
        }
        return res.json();
      })
      .then((data: TableResponse) => {
        setTableData(data);
        setEditedRows(new Map());
        setStatus({ type: "idle" });
      })
      .catch((err: Error) => {
        setStatus({ type: "error", message: err.message });
        setTableData(null);
      })
      .finally(() => setIsLoading(false));
  }, [selectedTable, limit, offset, reloadKey]);

  const meta = tableData?.meta;
  const primaryKey = meta?.primaryKey;

  const totalPages = useMemo(() => {
    if (!meta) return 1;
    return Math.max(1, Math.ceil(meta.rowCount / meta.limit));
  }, [meta]);

  const currentPage = useMemo(() => {
    if (!meta) return 1;
    return Math.floor(meta.offset / meta.limit) + 1;
  }, [meta]);

  function handleCellChange(row: Row, column: ColumnInfo, value: string) {
    if (!primaryKey) return;
    const keyField = primaryKey === "__rowid__" ? "__rowid__" : primaryKey;
    const identifier = row[keyField];
    if (typeof identifier !== "string" && typeof identifier !== "number") {
      return;
    }
    const updatedRow = { ...row, [column.name]: value };
    const newEdited = new Map(editedRows);
    newEdited.set(identifier, updatedRow);
    setEditedRows(newEdited);
    setTableData((prev) => {
      if (!prev) return prev;
      const nextRows = prev.rows.map((r) => {
        const rIdentifier = r[keyField];
        if (rIdentifier === identifier) {
          return updatedRow;
        }
        return r;
      });
      return { ...prev, rows: nextRows };
    });
  }

  async function handleSave() {
    if (!selectedTable || editedRows.size === 0 || !primaryKey) {
      return;
    }
    setStatus({ type: "loading", message: "Değişiklikler kaydediliyor..." });
    try {
      const updates = Array.from(editedRows.entries()).map(([key, row]) => {
        const data: Record<string, unknown> = {};
        meta?.columns.forEach((column) => {
          if (column.name === primaryKey) return;
          if (Object.prototype.hasOwnProperty.call(row, column.name)) {
            data[column.name] = row[column.name];
          }
        });
        return { key, data };
      });

      const response = await fetch(`/api/tables/${selectedTable}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates }),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error?.error || "Güncelleme başarısız");
      }
      setStatus({ type: "success", message: "Değişiklikler kaydedildi" });
      setEditedRows(new Map());
      setReloadKey((key) => key + 1);
    } catch (error) {
      setStatus({
        type: "error",
        message: error instanceof Error ? error.message : "Güncelleme başarısız",
      });
    }
  }

  function renderCell(row: Row, column: ColumnInfo) {
    const value = row[column.name];
    const stringValue = value === null || typeof value === "undefined" ? "" : String(value);
    const isPrimary = primaryKey && column.name === primaryKey;
    return (
      <Input
        value={stringValue}
        onChange={(event) => handleCellChange(row, column, event.target.value)}
        disabled={isPrimary}
        className={isPrimary ? "bg-muted" : undefined}
      />
    );
  }

  function handleLimitChange(event: React.ChangeEvent<HTMLInputElement>) {
    const next = Number(event.target.value) || DEFAULT_LIMIT;
    setLimit(Math.max(1, Math.min(500, next)));
    setOffset(0);
  }

  function goToPage(page: number) {
    if (!meta) return;
    const safePage = Math.min(Math.max(page, 1), totalPages);
    setOffset((safePage - 1) * meta.limit);
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-6xl flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-2 border-b pb-6 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold">
            <DatabaseIcon className="h-6 w-6" /> Veritabanı Kontrol Paneli
          </h1>
          <p className="text-sm text-muted-foreground">
            INCIScraper veritabanındaki tabloları inceleyin, filtreleyin ve düzenleyin.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label className="text-sm font-medium" htmlFor="table-select">
            Tablo
          </label>
          <select
            id="table-select"
            className="h-10 rounded-md border border-input bg-background px-3 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            value={selectedTable}
            onChange={(event) => {
              setSelectedTable(event.target.value);
              setOffset(0);
            }}
          >
            {tables.map((table) => (
              <option key={table} value={table}>
                {table}
              </option>
            ))}
          </select>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={() => {
              setReloadKey((key) => key + 1);
            }}
            disabled={!selectedTable}
          >
            <RefreshCcw className="h-4 w-4" />
          </Button>
        </div>
      </header>

      {status.type !== "idle" && (
        <div
          className={`flex items-center gap-2 rounded-md border px-3 py-2 text-sm ${
            status.type === "error"
              ? "border-destructive/60 bg-destructive/10 text-destructive"
              : status.type === "success"
              ? "border-green-500/40 bg-green-500/10 text-green-700"
              : "border-muted bg-muted/40 text-muted-foreground"
          }`}
        >
          {status.type === "loading" && <Loader2 className="h-4 w-4 animate-spin" />}
          {status.message ?? (status.type === "success" ? "İşlem tamamlandı" : undefined)}
        </div>
      )}

      {tableData && meta ? (
        <section className="space-y-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <span>{meta.rowCount} kayıt</span>
              <span>•</span>
              <span>Sayfa {currentPage} / {totalPages}</span>
              {editedRows.size > 0 && (
                <Badge variant="secondary">{editedRows.size} satır değişti</Badge>
              )}
            </div>
            <div className="flex items-center gap-2">
              <label className="text-sm" htmlFor="limit-input">
                Sayfa boyutu
              </label>
              <Input
                id="limit-input"
                type="number"
                min={1}
                max={500}
                value={limit}
                onChange={handleLimitChange}
                className="w-24"
              />
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => goToPage(currentPage - 1)}
                  disabled={currentPage <= 1 || isLoading}
                >
                  Önceki
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => goToPage(currentPage + 1)}
                  disabled={currentPage >= totalPages || isLoading}
                >
                  Sonraki
                </Button>
              </div>
              <Button
                type="button"
                onClick={handleSave}
                disabled={editedRows.size === 0 || isLoading}
              >
                <Save className="mr-2 h-4 w-4" /> Kaydet
              </Button>
            </div>
          </div>
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  {meta.columns.map((column) => (
                    <TableHead key={column.cid}>
                      <div className="flex flex-col">
                        <span>{column.name}</span>
                        <span className="text-xs text-muted-foreground">{column.type}</span>
                      </div>
                    </TableHead>
                  ))}
                </TableRow>
              </TableHeader>
              <TableBody>
                {tableData.rows.map((row, rowIndex) => (
                  <TableRow key={rowIndex}>
                    {meta.columns.map((column) => (
                      <TableCell key={column.cid}>{renderCell(row, column)}</TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
              <TableCaption>
                Tabloda en fazla 500 kayıt görüntülenebilir. Daha fazla satır için sayfa boyutunu ve sayfa numarasını değiştirin.
              </TableCaption>
            </Table>
          </div>
        </section>
      ) : (
        <div className="flex min-h-[200px] items-center justify-center rounded-md border border-dashed">
          {isLoading ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> Tablo yükleniyor...
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              Görüntülenecek tablo seçin veya veritabanının oluşturulduğundan emin olun.
            </p>
          )}
        </div>
      )}
    </main>
  );
}
