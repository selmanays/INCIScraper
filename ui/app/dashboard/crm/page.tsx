import CalendarDateRangePicker from "@/components/date-range-picker";
import { Button } from "@/components/ui/button";
import { generateMeta } from "@/lib/utils";

import { LeadBySourceCard } from "./cards/lead-by-source-card";
import { LeadsTable } from "./cards/leads-table";
import { RecentTasks } from "./cards/recent-tasks";
import { SalesPipelineCard } from "./cards/sales-pipeline-card";
import { SummaryCards } from "./cards/summary-cards";
import { TargetCard } from "./cards/target-card";

export async function generateMetadata() {
  return generateMeta({
    title: "CRM Dashboard - INCIScraper",
    description:
      "INCIScraper satış ekiplerinin lead performansını takip edebileceği CRM panosu.",
    canonical: "/dashboard/crm"
  });
}

export default function Page() {
  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <h1 className="text-2xl font-bold tracking-tight">CRM Dashboard</h1>
        <div className="flex items-center gap-2">
          <CalendarDateRangePicker />
          <Button>Raporu indir</Button>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <TargetCard />
        <SummaryCards />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <LeadBySourceCard />
        <RecentTasks />
        <SalesPipelineCard />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <LeadsTable />
      </div>
    </div>
  );
}
