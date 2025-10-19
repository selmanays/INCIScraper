"use client";

import { useState } from "react";
import { CirclePlus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { cn } from "@/lib/utils";

type Task = {
  id: string;
  title: string;
  description: string;
  due: string;
  priority: "low" | "medium" | "high";
  completed: boolean;
};

const tasks: Task[] = [
  {
    id: "1",
    title: "Acme Inc. ile takip",
    description: "Teklif gönderin ve toplantı planlayın",
    due: "Bugün",
    priority: "high",
    completed: false
  },
  {
    id: "2",
    title: "Çeyrek raporu",
    description: "Satış verilerini derleyin",
    due: "Yarın",
    priority: "medium",
    completed: false
  },
  {
    id: "3",
    title: "Müşteri profillerini güncelle",
    description: "İletişim bilgilerini doğrulayın",
    due: "15 Ekim",
    priority: "low",
    completed: true
  }
];

const priorityStyles: Record<Task["priority"], string> = {
  high: "bg-red-100 text-red-700",
  medium: "bg-amber-100 text-amber-700",
  low: "bg-green-100 text-green-700"
};

export function RecentTasks() {
  const [items, setItems] = useState(tasks);

  return (
    <Card className="flex h-full flex-col gap-6">
      <CardHeader className="flex flex-row items-start justify-between">
        <div>
          <CardTitle>Görevler</CardTitle>
          <CardDescription>Yaklaşan işleri takip edin ve tamamlayın.</CardDescription>
        </div>
        <Button variant="outline" size="sm" className="gap-1">
          <CirclePlus className="h-4 w-4" /> Görev ekle
        </Button>
      </CardHeader>
      <CardContent className="space-y-3">
        {items.map((task) => (
          <div
            key={task.id}
            className={cn(
              "flex items-start gap-3 rounded-md border p-3 transition-colors",
              task.completed && "bg-muted/50"
            )}
          >
            <Checkbox
              checked={task.completed}
              onCheckedChange={() =>
                setItems((prev) =>
                  prev.map((entry) =>
                    entry.id === task.id
                      ? { ...entry, completed: !entry.completed }
                      : entry
                  )
                )
              }
              className="mt-1"
            />
            <div className="space-y-1">
              <p
                className={cn(
                  "text-sm font-medium leading-none",
                  task.completed && "line-through text-muted-foreground"
                )}
              >
                {task.title}
              </p>
              <p className={cn("text-xs text-muted-foreground", task.completed && "line-through")}>
                {task.description}
              </p>
              <div className="flex items-center gap-2 pt-1">
                <span className={cn("rounded-full px-2 py-0.5 text-xs font-medium", priorityStyles[task.priority])}>
                  {task.priority === "high"
                    ? "Yüksek"
                    : task.priority === "medium"
                    ? "Orta"
                    : "Düşük"}
                </span>
                <span className="text-xs text-muted-foreground">Teslim: {task.due}</span>
              </div>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
