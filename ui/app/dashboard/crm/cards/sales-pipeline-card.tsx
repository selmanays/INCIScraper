import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const pipeline = [
  {
    stage: "Lead",
    deals: 235,
    value: 420500,
    color: "var(--chart-1)",
    percent: 38
  },
  {
    stage: "Qualified",
    deals: 146,
    value: 267800,
    color: "var(--chart-2)",
    percent: 24
  },
  {
    stage: "Proposal",
    deals: 84,
    value: 192400,
    color: "var(--chart-3)",
    percent: 18
  },
  {
    stage: "Negotiation",
    deals: 52,
    value: 129600,
    color: "var(--chart-4)",
    percent: 12
  },
  {
    stage: "Closed Won",
    deals: 36,
    value: 87200,
    color: "var(--chart-5)",
    percent: 8
  }
];

export function SalesPipelineCard() {
  return (
    <Card className="flex flex-col">
      <CardHeader>
        <CardTitle>Satış hunisi</CardTitle>
        <CardDescription>Aktif fırsatların aşamalara göre dağılımı.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex h-4 w-full overflow-hidden rounded-full">
          {pipeline.map((stage) => (
            <div
              key={stage.stage}
              className="h-full"
              style={{ width: `${stage.percent}%`, backgroundColor: stage.color }}
            />
          ))}
        </div>
        <div className="space-y-4">
          {pipeline.map((stage) => (
            <div key={stage.stage} className="flex items-center gap-4">
              <span className="h-3 w-3 rounded-full" style={{ backgroundColor: stage.color }} />
              <div className="flex flex-1 items-center justify-between">
                <div>
                  <p className="text-sm font-medium">{stage.stage}</p>
                  <p className="text-xs text-muted-foreground">
                    {stage.deals} fırsat ·
                    {" "}
                    {new Intl.NumberFormat("tr-TR", {
                      style: "currency",
                      currency: "USD"
                    }).format(stage.value)}
                  </p>
                </div>
                <div className="flex w-24 items-center gap-2">
                  <div className="relative h-2 flex-1 overflow-hidden rounded-full bg-primary/20">
                    <div
                      className="absolute inset-y-0 left-0 rounded-full"
                      style={{
                        width: `${stage.percent}%`,
                        backgroundColor: stage.color
                      }}
                    />
                  </div>
                  <span className="text-xs text-muted-foreground w-10 text-right">{stage.percent}%</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
