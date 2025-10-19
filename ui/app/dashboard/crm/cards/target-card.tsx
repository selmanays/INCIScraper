"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart";
import {
  PolarAngleAxis,
  PolarGrid,
  RadialBar,
  RadialBarChart
} from "recharts";

const chartConfig = {
  progress: {
    label: "Target completion",
    color: "hsl(var(--primary))"
  }
} satisfies ChartConfig;

const data = [{ name: "progress", progress: 48 }];

export function TargetCard() {
  return (
    <Card className="flex flex-col gap-2">
      <CardHeader>
        <CardTitle className="font-display text-xl">Hedef tamamlanma durumu</CardTitle>
        <CardDescription>
          Kampanya hedefinin yüzde kaçının tamamlandığını takip edin.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="flex items-center gap-4">
          <div className="relative mx-auto h-[120px] w-[120px]">
            <ChartContainer config={chartConfig} className="h-full w-full">
              <RadialBarChart
                data={data}
                innerRadius={70}
                outerRadius={100}
                startAngle={90}
                endAngle={-270}
              >
                <PolarGrid radialLines={false} stroke="none" />
                <PolarAngleAxis type="number" domain={[0, 100]} tick={false} />
                <RadialBar
                  dataKey="progress"
                  cornerRadius={100}
                  background
                  fill="var(--color-progress)"
                />
                <ChartTooltip cursor={false} content={<ChartTooltipContent hideLabel />} />
              </RadialBarChart>
            </ChartContainer>
            <div className="absolute inset-0 flex flex-col items-center justify-center">
              <span className="font-display text-2xl">%48</span>
              <span className="text-xs text-muted-foreground">Tamamlandı</span>
            </div>
          </div>
          <div className="space-y-2 text-sm text-muted-foreground">
            <p>
              <span className="font-semibold text-primary">%48</span> tamamlandı. Güncel durumu
              kontrol ederek eksik kalan işleri hızla kapatabilirsiniz.
            </p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
