import { BriefcaseBusiness, UsersRound, WalletMinimal } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const summaryCards = [
  {
    title: "Toplam müşteri",
    value: "1.890",
    change: "+10.4%",
    changeTone: "positive" as const,
    description: "Geçen aya göre",
    icon: UsersRound
  },
  {
    title: "Toplam anlaşma",
    value: "102.890",
    change: "-0.8%",
    changeTone: "negative" as const,
    description: "Geçen aya göre",
    icon: BriefcaseBusiness
  },
  {
    title: "Toplam gelir",
    value: "$435.578",
    change: "+20.1%",
    changeTone: "positive" as const,
    description: "Geçen aya göre",
    icon: WalletMinimal
  }
];

export function SummaryCards() {
  return summaryCards.map((card) => {
    const Icon = card.icon;
    const changeClass =
      card.changeTone === "positive" ? "text-emerald-600" : "text-rose-600";

    return (
      <Card key={card.title} className="flex flex-col gap-6 rounded-xl border py-6">
        <CardHeader className="px-6">
          <CardDescription>{card.title}</CardDescription>
          <CardTitle className="font-display text-3xl">{card.value}</CardTitle>
          <p className="text-sm text-muted-foreground">
            <span className={changeClass}>{card.change}</span> {card.description}
          </p>
        </CardHeader>
        <CardContent className="px-6">
          <div className="bg-muted flex h-12 w-12 items-center justify-center rounded-full border">
            <Icon className="h-5 w-5" />
          </div>
        </CardContent>
      </Card>
    );
  });
}
