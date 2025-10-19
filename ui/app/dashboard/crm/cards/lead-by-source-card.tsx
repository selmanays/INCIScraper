"use client";

import { Pie, PieChart } from "recharts";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig
} from "@/components/ui/chart";
import { MoreHorizontal } from "lucide-react";

const data = [
  { source: "social", leads: 275 },
  { source: "email", leads: 200 },
  { source: "call", leads: 287 },
  { source: "other", leads: 173 }
];

const chartConfig = {
  social: {
    label: "Sosyal medya",
    color: "hsl(var(--chart-1))"
  },
  email: {
    label: "E-posta",
    color: "hsl(var(--chart-2))"
  },
  call: {
    label: "Çağrı merkezi",
    color: "hsl(var(--chart-3))"
  },
  other: {
    label: "Diğer",
    color: "hsl(var(--chart-4))"
  }
} satisfies ChartConfig;

export function LeadBySourceCard() {
  const total = data.reduce((acc, item) => acc + item.leads, 0);

  return (
    <Card className="flex flex-col">
      <CardHeader className="flex flex-row items-start justify-between">
        <CardTitle>Leads kaynak dağılımı</CardTitle>
        <Button variant="outline" size="sm" className="gap-1">
          <MoreHorizontal className="h-4 w-4" />
          Dışa aktar
        </Button>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-6">
        <ChartContainer className="mx-auto aspect-square max-h-[260px]" config={chartConfig}>
          <PieChart>
            <ChartTooltip content={<ChartTooltipContent hideLabel nameKey="source" />} />
            <Pie
              data={data}
              dataKey="leads"
              nameKey="source"
              innerRadius={70}
              strokeWidth={5}
              stroke="#fff"
            />
          </PieChart>
        </ChartContainer>
        <div className="text-center">
          <p className="font-display text-3xl">{total.toLocaleString("tr-TR")}</p>
          <p className="text-muted-foreground text-sm">Toplam lead</p>
        </div>
        <div className="grid grid-cols-2 gap-4 text-sm">
          {data.map((item) => {
            const config = chartConfig[item.source as keyof typeof chartConfig];
            return (
              <div key={item.source} className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span
                    className="block size-2 rounded-full"
                    style={{ backgroundColor: config.color }}
                  />
                  <span className="uppercase tracking-wide text-muted-foreground text-xs">
                    {config.label}
                  </span>
                </div>
                <span className="font-semibold">{item.leads}</span>
              </div>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
