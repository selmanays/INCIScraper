"use client";

import { useEffect, useMemo, useState } from "react";
import {
  BarChart3,
  Database,
  LayoutDashboard,
  Loader2,
  Menu,
  RefreshCcw,
  Save,
  Table2,
} from "lucide-react";

import { ThemeToggle } from "@/components/theme-toggle";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";

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

type ColumnTypeStat = {
  type: string;
  count: number;
};

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
  const [isSidebarOpen, setIsSidebarOpen] = useState(false);

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

  const columnStats: ColumnTypeStat[] = useMemo(() => {
    if (!meta) return [];
    const counts = new Map<string, number>();
    meta.columns.forEach((column) => {
      const typeName = column.type?.trim();
      const label = typeName && typeName.length > 0 ? typeName.toUpperCase() : "TANIMSIZ";
      counts.set(label, (counts.get(label) ?? 0) + 1);
    });
    return Array.from(counts.entries())
      .map(([type, count]) => ({ type, count }))
      .sort((a, b) => b.count - a.count);
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
    const stringValue =
      value === null || typeof value === "undefined" ? "" : String(value);
    const isPrimary = primaryKey && column.name === primaryKey;
    return (
      <Input
        value={stringValue}
        onChange={(event) => handleCellChange(row, column, event.target.value)}
        disabled={isPrimary}
        className={cn(isPrimary && "bg-muted text-muted-foreground")}
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

  function closeSidebar() {
    setIsSidebarOpen(false);
  }

  const sidebar = (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b px-6 py-5">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <Database className="h-5 w-5" />
        </div>
        <div>
          <p className="text-sm font-semibold">INCIScraper</p>
          <p className="text-xs text-muted-foreground">Veri kontrol paneli</p>
        </div>
      </div>
      <div className="flex-1 space-y-6 overflow-y-auto px-4 py-6">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Gezinme
          </p>
          <div className="mt-3 space-y-1">
            <button
              type="button"
              className="flex w-full items-center gap-2 rounded-lg bg-primary/10 px-3 py-2 text-sm font-medium text-primary"
            >
              <LayoutDashboard className="h-4 w-4" />
              Genel Bakış
            </button>
          </div>
        </div>
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Tablolar
            </p>
            <Badge variant="secondary">{tables.length}</Badge>
          </div>
          <div className="space-y-1">
            {tables.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                Henüz tablo bulunamadı.
              </p>
            ) : (
              tables.map((table) => {
                const isActive = table === selectedTable;
                return (
                  <button
                    key={table}
                    type="button"
                    onClick={() => {
                      setSelectedTable(table);
                      setOffset(0);
                      closeSidebar();
                    }}
                    className={cn(
                      "flex w-full items-center justify-between rounded-lg px-3 py-2 text-sm transition",
                      isActive
                        ? "bg-primary text-primary-foreground shadow-sm"
                        : "hover:bg-muted",
                    )}
                  >
                    <span className="flex items-center gap-2 truncate">
                      <Table2 className="h-4 w-4 shrink-0" />
                      <span className="truncate">{table}</span>
                    </span>
                    {isActive && meta && (
                      <Badge variant="secondary" className="ml-2">
                        {meta.rowCount}
                      </Badge>
                    )}
                  </button>
                );
              })
            )}
          </div>
        </div>
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Özet
          </p>
          <div className="mt-3 space-y-1 rounded-lg border bg-muted/40 p-3 text-xs text-muted-foreground">
            <p>
              {selectedTable
                ? `${selectedTable} tablosunda ${meta?.rowCount ?? 0} kayıt bulunuyor.`
                : "Görüntülemek için bir tablo seçin."}
            </p>
            <p>
              {meta
                ? `Sayfa ${currentPage} / ${totalPages} • Limit ${meta.limit}`
                : "Sayfa bilgisi tablo seçildiğinde görüntülenir."}
            </p>
          </div>
        </div>
      </div>
      <div className="border-t px-6 py-4 text-xs text-muted-foreground">
        <p>Son yenileme anahtarı: {reloadKey}</p>
      </div>
    </div>
  );

  const rowCount = meta?.rowCount ?? 0;
  const columnCount = meta?.columns.length ?? 0;
  const tableCount = tables.length;
  const editedCount = editedRows.size;

  return (
    <div className="flex min-h-screen w-full bg-muted/30">
      <div className="hidden w-72 border-r bg-background lg:flex lg:flex-col">
        {sidebar}
      </div>
      {isSidebarOpen && (
        <>
          <div
            className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm lg:hidden"
            onClick={closeSidebar}
          />
          <div className="fixed inset-y-0 left-0 z-50 w-72 border-r bg-background shadow-lg lg:hidden">
            {sidebar}
          </div>
        </>
      )}
      <div className="flex min-h-screen flex-1 flex-col lg:ml-0">
        <header className="flex h-16 items-center gap-4 border-b bg-background px-4 sm:px-6">
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="lg:hidden"
            onClick={() => setIsSidebarOpen(true)}
          >
            <Menu className="h-5 w-5" />
            <span className="sr-only">Menüyü aç</span>
          </Button>
          <div className="flex flex-col">
            <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Kontrol Paneli
            </span>
            <span className="text-base font-semibold">
              INCIScraper veri görünümleri
            </span>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <div className="hidden items-center gap-2 rounded-md border bg-background px-2 py-1 text-sm sm:flex">
              <label className="text-xs text-muted-foreground" htmlFor="table-select">
                Tablo
              </label>
              <select
                id="table-select"
                className="h-8 rounded-md bg-transparent px-2 text-sm focus-visible:outline-none"
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
            </div>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={() => setReloadKey((key) => key + 1)}
              disabled={!selectedTable}
            >
              <RefreshCcw className="h-4 w-4" />
              <span className="sr-only">Yenile</span>
            </Button>
            <ThemeToggle />
          </div>
        </header>
        <main className="flex-1">
          <div className="@container/main flex flex-1 flex-col gap-6 p-4 sm:p-6">
            <SummaryCards
              tableCount={tableCount}
              rowCount={rowCount}
              columnCount={columnCount}
              editedCount={editedCount}
              currentTable={selectedTable}
              isLoading={isLoading}
            />
            <div className="grid gap-6 xl:grid-cols-[minmax(0,1.7fr)_minmax(0,1fr)]">
              <Card className="overflow-hidden">
                <CardHeader className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
                  <div>
                    <CardTitle className="text-lg font-semibold">
                      {selectedTable ? selectedTable : "Tablo seçilmedi"}
                    </CardTitle>
                    <CardDescription>
                      Verileri filtreleyin, düzenleyin ve kaydedin.
                    </CardDescription>
                  </div>
                  <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:justify-end">
                    <div className="flex items-center gap-2">
                      <label className="text-xs text-muted-foreground" htmlFor="limit-input">
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
                    </div>
                    <div className="flex items-center gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => goToPage(currentPage - 1)}
                        disabled={currentPage <= 1 || isLoading || !meta}
                      >
                        Önceki
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => goToPage(currentPage + 1)}
                        disabled={currentPage >= totalPages || isLoading || !meta}
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
                </CardHeader>
                <CardContent className="space-y-4">
                  {status.type !== "idle" && (
                    <StatusBanner status={status} />
                  )}
                  {meta && tableData ? (
                    <div className="space-y-4">
                      <div className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
                        <span>{meta.rowCount} kayıt</span>
                        <span>•</span>
                        <span>
                          Sayfa {currentPage} / {totalPages}
                        </span>
                        <span>•</span>
                        <span>{meta.columns.length} kolon</span>
                        {editedRows.size > 0 && (
                          <Badge variant="secondary">
                            {editedRows.size} satır değişti
                          </Badge>
                        )}
                      </div>
                      <div className="-mx-6 overflow-x-auto px-6">
                        <Table>
                          <TableHeader>
                            <TableRow>
                              {meta.columns.map((column) => (
                                <TableHead key={column.cid} className="min-w-[160px]">
                                  <div className="flex flex-col">
                                    <span className="font-semibold text-foreground">
                                      {column.name}
                                    </span>
                                    <span className="text-xs text-muted-foreground">
                                      {column.type || "Tanımsız"}
                                    </span>
                                  </div>
                                </TableHead>
                              ))}
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {tableData.rows.map((row, rowIndex) => (
                              <TableRow key={rowIndex} className="bg-background">
                                {meta.columns.map((column) => (
                                  <TableCell key={column.cid} className="align-top">
                                    {renderCell(row, column)}
                                  </TableCell>
                                ))}
                              </TableRow>
                            ))}
                          </TableBody>
                          <TableCaption>
                            En fazla 500 kayıt görüntülenebilir. Daha fazla satır
                            için sayfa boyutunu ve sayfa numarasını değiştirin.
                          </TableCaption>
                        </Table>
                      </div>
                    </div>
                  ) : (
                    <div className="flex min-h-[240px] items-center justify-center rounded-lg border border-dashed">
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
                </CardContent>
              </Card>
              <div className="space-y-6">
                <Card>
                  <CardHeader>
                    <CardTitle className="flex items-center gap-2 text-base">
                      <BarChart3 className="h-4 w-4 text-primary" /> Kolon dağılımı
                    </CardTitle>
                    <CardDescription>
                      Kolon türlerine göre tablo yapısını inceleyin.
                    </CardDescription>
                  </CardHeader>
                  <CardContent>
                    {columnStats.length === 0 ? (
                      <p className="text-sm text-muted-foreground">
                        Kolon bilgisi tablo seçildiğinde görüntülenir.
                      </p>
                    ) : (
                      <ColumnDistribution stats={columnStats} />
                    )}
                  </CardContent>
                </Card>
                <Card>
                  <CardHeader>
                    <CardTitle className="text-base">Tablo özeti</CardTitle>
                    <CardDescription>
                      Birincil anahtar ve satır kimliği kullanımı dahil temel bilgiler.
                    </CardDescription>
                  </CardHeader>
                  <CardContent>
                    {meta ? (
                      <dl className="space-y-3 text-sm">
                        <div className="flex items-center justify-between">
                          <dt className="text-muted-foreground">Birincil anahtar</dt>
                          <dd className="font-medium text-foreground">
                            {meta.primaryKey || "Tanımsız"}
                          </dd>
                        </div>
                        <div className="flex items-center justify-between">
                          <dt className="text-muted-foreground">Satır kimliği</dt>
                          <dd className="font-medium text-foreground">
                            {meta.usesRowId ? "Kullanılıyor" : "Kullanılmıyor"}
                          </dd>
                        </div>
                        <div className="flex items-center justify-between">
                          <dt className="text-muted-foreground">Limit</dt>
                          <dd className="font-medium text-foreground">{meta.limit}</dd>
                        </div>
                        <div className="flex items-center justify-between">
                          <dt className="text-muted-foreground">Offset</dt>
                          <dd className="font-medium text-foreground">{meta.offset}</dd>
                        </div>
                      </dl>
                    ) : (
                      <p className="text-sm text-muted-foreground">
                        Bir tablo seçildiğinde özet bilgileri burada görüntülenir.
                      </p>
                    )}
                  </CardContent>
                </Card>
              </div>
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}

function SummaryCards({
  tableCount,
  rowCount,
  columnCount,
  editedCount,
  currentTable,
  isLoading,
}: {
  tableCount: number;
  rowCount: number;
  columnCount: number;
  editedCount: number;
  currentTable: string;
  isLoading: boolean;
}) {
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Toplam tablo
          </CardTitle>
          <CardDescription>Veritabanında yer alan tablo sayısı.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="text-3xl font-semibold text-foreground">{tableCount}</div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Seçili tablo
          </CardTitle>
          <CardDescription>İşlem yaptığınız aktif tablo.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="text-lg font-semibold text-foreground">
            {currentTable || "Seçilmedi"}
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Toplam kayıt
          </CardTitle>
          <CardDescription>Tablodaki toplam satır sayısı.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="text-3xl font-semibold text-foreground">
            {isLoading ? "..." : rowCount}
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-sm font-medium text-muted-foreground">
            Kolon & değişiklik
          </CardTitle>
          <CardDescription>Kalan kolon sayısı ve bekleyen düzenlemeler.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex items-baseline gap-3">
            <span className="text-3xl font-semibold text-foreground">{columnCount}</span>
            <Badge variant={editedCount > 0 ? "default" : "secondary"}>
              {editedCount} değişiklik
            </Badge>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function ColumnDistribution({ stats }: { stats: ColumnTypeStat[] }) {
  const maxCount = stats.reduce((max, stat) => Math.max(max, stat.count), 0);
  return (
    <div className="space-y-4">
      {stats.map((stat) => {
        const width = maxCount ? Math.round((stat.count / maxCount) * 100) : 0;
        return (
          <div key={stat.type} className="space-y-2">
            <div className="flex items-center justify-between text-sm font-medium">
              <span>{stat.type}</span>
              <span className="text-muted-foreground">{stat.count}</span>
            </div>
            <div className="h-2 w-full rounded-full bg-muted">
              <div
                className="h-2 rounded-full bg-primary transition-[width]"
                style={{ width: `${width}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function StatusBanner({ status }: { status: StatusMessage }) {
  const colorClasses =
    status.type === "error"
      ? "border-destructive/60 bg-destructive/10 text-destructive"
      : status.type === "success"
      ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
      : "border-muted bg-muted/60 text-muted-foreground";

  return (
    <div className={cn("flex items-center gap-2 rounded-lg border px-3 py-2 text-sm", colorClasses)}>
      {status.type === "loading" && (
        <Loader2 className="h-4 w-4 animate-spin" />
      )}
      <span>
        {status.message ??
          (status.type === "success" ? "İşlem tamamlandı" : undefined)}
      </span>
    </div>
  );
}
